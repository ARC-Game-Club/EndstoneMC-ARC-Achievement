# -*- coding: utf-8 -*-
"""弧光成就插件：击杀成就统计、玩家/OP 面板，依赖 arc_core 头衔与经济 API。"""

import json
import shutil
from pathlib import Path
from typing import Any, Optional

from endstone import Player
from endstone.event import event_handler, ActorDeathEvent, BlockBreakEvent, BlockPlaceEvent, PlayerDeathEvent
from endstone.form import ActionForm, ModalForm, TextInput, Dropdown
from endstone.plugin import Plugin
from endstone.command import Command, CommandSender

from endstone_arc_achievement.AchievementSystem import AchievementSystem
from endstone_arc_achievement.LanguageManager import LanguageManager

MAIN_PATH = "plugins/ARCAchievement"
LEGACY_JSON = Path("plugins/ARCCore/achievements.json")


def normalize_entity_type_id(entity_type: str) -> str:
    s = str(entity_type or "").strip()
    if not s:
        return ""
    if ":" in s:
        ns, name = s.split(":", 1)
        return f"{ns.lower()}:{name.lower()}"
    return s.lower()


class _TitleBridge:
    """把 arc_core 的 api_* 适配成 AchievementSystem 期望的 title_system 接口。"""

    def __init__(self, arc):
        self._arc = arc

    def has_unlocked_title_by_xuid(self, xuid: str, title: str, rarity: str = "普通") -> bool:
        try:
            return bool(
                self._arc.api_has_unlocked_title(
                    title, xuid=str(xuid or ""), rarity=rarity
                )
            )
        except TypeError:
            # 兼容旧核心：无 rarity 参数
            return bool(self._arc.api_has_unlocked_title(title, xuid=str(xuid or "")))

    def has_title_definition(self, title: str, rarity: str = "普通") -> bool:
        has_fn = getattr(self._arc, "api_has_title_definition", None)
        if callable(has_fn):
            try:
                return bool(has_fn(title, rarity))
            except TypeError:
                defn = self.get_title_definition(title, rarity)
                if defn is None:
                    return False
                return str(defn.get("rarity") or "普通").strip() == str(rarity or "普通").strip()
        defn = self.get_title_definition(title, rarity)
        if defn is None:
            return False
        return str(defn.get("rarity") or "普通").strip() == str(rarity or "普通").strip()

    def ensure_title_definition(
        self,
        title: str,
        rarity: str = "普通",
        description: str = "",
        reward_money: float = 0.0,
        reward_items=None,
    ) -> bool:
        _ = reward_money
        _ = reward_items
        return bool(
            self._arc.api_ensure_title_definition(
                title, rarity, description, 0.0, []
            )
        )

    def set_title_definition(
        self, title: str, rarity: str, description: str, reward_money: float = 0.0, reward_items=None
    ) -> bool:
        _ = reward_money
        _ = reward_items
        return bool(
            self._arc.api_set_title_definition(
                title, rarity, description, 0.0, []
            )
        )

    def get_title_definition(self, title: str, rarity: str = None):
        get_fn = getattr(self._arc, "api_get_title_definition", None)
        if not callable(get_fn):
            return None
        try:
            return get_fn(title, rarity)
        except TypeError:
            return get_fn(title)


class ARCAchievementPlugin(Plugin):
    api_version = "0.10"
    commands = {
        "ach": {
            "description": "打开弧光成就菜单",
            "usages": ["/ach"],
            "permissions": ["arc_achievement.command.ach"],
        },
        "achop": {
            "description": "打开弧光成就 OP 管理（仅 OP）",
            "usages": ["/achop"],
            "permissions": ["arc_achievement.command.achop"],
        },
    }
    permissions = {
        "arc_achievement.command.ach": {
            "description": "打开成就菜单",
            "default": True,
        },
        "arc_achievement.command.achop": {
            "description": "打开成就 OP 面板",
            "default": "op",
        },
    }

    def __init__(self):
        super().__init__()
        self.arc_core = None
        self.achievement_system: Optional[AchievementSystem] = None
        self.language_manager: Optional[LanguageManager] = None
        self._title_bridge: Optional[_TitleBridge] = None

    def on_load(self) -> None:
        Path(MAIN_PATH).mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_json_if_needed()
        self.language_manager = LanguageManager("ZH-CN")

    def on_enable(self) -> None:
        self.register_events(self)
        self.arc_core = self.server.plugin_manager.get_plugin("arc_core")
        if self.arc_core is None:
            self.logger.error("[ARCAchievement] 未找到 arc_core，成就插件已禁用相关功能。")
            return
        self._title_bridge = _TitleBridge(self.arc_core)

        def _unlock_title_with_rarity(player, title, rarity="普通"):
            try:
                return self.arc_core.api_unlock_title(player, title, rarity=rarity)
            except TypeError:
                return self.arc_core.api_unlock_title(player, title)

        self.achievement_system = AchievementSystem(
            self.arc_core,
            self._title_bridge,
            self.language_manager,
            _unlock_title_with_rarity,
            MAIN_PATH,
            self._announce_achievement_unlock,
            grant_money_func=self.arc_core.increase_player_money,
            grant_items_func=self.arc_core.api_give_player_items,
        )
        self.achievement_system.ensure_tables()
        try:
            migrated = self.achievement_system.backfill_meta_from_title_definitions()
            if migrated:
                self.logger.info(
                    f"[ARCAchievement] 已从核心头衔定义回填 {migrated} 条成就的稀有度/介绍/奖励。"
                )
        except Exception as e:
            self.logger.error(f"[ARCAchievement] backfill achievement meta error: {e}")
        self.logger.info(
            "[ARCAchievement] 已启用；活动统计经 arc_core API 查询，解锁标记存 plugins/ARCAchievement/achievement.db。"
        )

    def on_disable(self) -> None:
        self.logger.info("[ARCAchievement] on_disable")

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if not isinstance(sender, Player):
            return True
        if self.achievement_system is None:
            sender.send_message("[弧光成就] 未加载 arc_core，无法使用成就功能。")
            return True
        name = command.name
        if name == "ach":
            self.show_my_achievements_hub(sender)
            return True
        if name == "achop":
            if not sender.is_op:
                sender.send_message("[弧光成就] 需要 OP 权限。")
                return True
            self.show_op_achievement_manage_panel(sender)
            return True
        return False

    def _migrate_legacy_json_if_needed(self) -> None:
        dest = Path(MAIN_PATH) / "achievements.json"
        if dest.exists():
            return
        if LEGACY_JSON.exists():
            try:
                shutil.copy2(LEGACY_JSON, dest)
            except Exception:
                pass

    def _get_arc(self):
        if self.arc_core is None:
            self.arc_core = self.server.plugin_manager.get_plugin("arc_core")
        return self.arc_core

    def return_to_my_info(self, player: Player):
        arc = self._get_arc()
        if arc is not None and hasattr(arc, "show_my_info_panel"):
            arc.show_my_info_panel(player)

    def return_to_op_main(self, player: Player):
        arc = self._get_arc()
        if arc is not None and hasattr(arc, "show_op_main_panel"):
            arc.show_op_main_panel(player)

    def _entity_display_name(self, entity_type_id: str) -> str:
        arc = self._get_arc()
        raw = str(entity_type_id or "").strip()
        if not raw:
            return raw
        try:
            edm = getattr(arc, "entity_display_name_manager", None) if arc else None
            if edm is not None:
                return edm.get_display_name_or_identifier(raw)
        except Exception:
            pass
        return raw

    def _format_money_display(self, value: float) -> str:
        arc = self._get_arc()
        try:
            if arc is not None and hasattr(arc, "_format_money_display"):
                return arc._format_money_display(value)
        except Exception:
            pass
        try:
            return f"{float(value):.2f}".rstrip("0").rstrip(".")
        except Exception:
            return str(value)

    def _get_title_rarity_color(self, title: str) -> str:
        arc = self._get_arc()
        try:
            ts = getattr(arc, "title_system", None) if arc else None
            if ts is not None:
                return ts.get_title_rarity_color(title)
        except Exception:
            pass
        return "§f"

    def _get_equipped_title(self, player: Player):
        arc = self._get_arc()
        try:
            ts = getattr(arc, "title_system", None) if arc else None
            if ts is not None:
                return ts.get_equipped_title(player)
        except Exception:
            pass
        return None

    def _format_player_display_label(self, name: str, equipped, xuid: str) -> str:
        arc = self._get_arc()
        try:
            if arc is not None and hasattr(arc, "format_player_display_label_with_guild"):
                return arc.format_player_display_label_with_guild(name, equipped, xuid)
        except Exception:
            pass
        return name or ""

    def _format_player_broadcast_display(self, player: Player) -> str:
        arc = self._get_arc()
        try:
            if arc is not None and hasattr(arc, "_format_death_broadcast_player_display"):
                return arc._format_death_broadcast_player_display(player)
        except Exception:
            pass
        equipped = self._get_equipped_title(player)
        return self._format_player_display_label(
            getattr(player, "name", "") or "",
            equipped,
            str(getattr(player, "xuid", "") or ""),
        )

    def _notify_qqsync(self, event_type: str, display_name: str, raw_name: str, message: str) -> None:
        arc = self._get_arc()
        try:
            if arc is not None and hasattr(arc, "_notify_qqsync"):
                arc._notify_qqsync(event_type, display_name, raw_name, message)
                return
        except Exception:
            pass
        try:
            pm = self.server.plugin_manager
            # Endstone 会把 entry-point 里的 '-' 转成 '_'，故优先 arc_qq_sync_astrbot
            for name in (
                "arc_qq_sync_astrbot",
                "arc-qq-sync-astrbot",
                "qqsync_plugin",
            ):
                plug = pm.get_plugin(name)
                if plug is not None and hasattr(plug, "api_send_event"):
                    plug.api_send_event(event_type, display_name, raw_name, message)
                    return
        except Exception:
            pass

    def _schedule_activity_check(self, callback) -> None:
        """延迟 0 tick，确保同 tick 内弧光核心已写入活动统计。"""
        try:
            self.server.scheduler.run_task(self, callback, delay=0)
        except Exception:
            try:
                callback()
            except Exception:
                pass

    @event_handler
    def on_actor_death(self, event: ActorDeathEvent):
        if self.achievement_system is None:
            return
        try:
            damage_source = getattr(event, "damage_source", None)
            killer = getattr(damage_source, "actor", None) if damage_source is not None else None
            if killer is None:
                return
            if getattr(killer, "type", None) != "minecraft:player":
                return
            dead_actor = getattr(event, "actor", None)
            if dead_actor is None:
                return
            if getattr(dead_actor, "type", None) == "minecraft:player":
                return
            dead_type = getattr(dead_actor, "type", None) or getattr(dead_actor, "identifier", None) or ""
            if not dead_type:
                return
            dead_type_key = normalize_entity_type_id(str(dead_type))
            self._schedule_activity_check(
                lambda k=killer, t=dead_type_key: self.achievement_system.record_kill(k, t)
            )
        except Exception as e:
            try:
                self.logger.error(f"[ARCAchievement] on_actor_death error: {e}")
            except Exception:
                pass

    @event_handler
    def on_player_death(self, event: PlayerDeathEvent):
        if self.achievement_system is None:
            return
        try:
            damage_source = getattr(event, "damage_source", None)
            killer = getattr(damage_source, "actor", None) if damage_source is not None else None
            if killer is None:
                for attr in ("killer", "damager"):
                    killer = getattr(event, attr, None)
                    if killer is not None:
                        break
            if killer is None:
                return
            if not (
                isinstance(killer, Player)
                or getattr(killer, "type", None) == "minecraft:player"
            ):
                return
            self._schedule_activity_check(
                lambda k=killer: self.achievement_system.record_kill(k, "minecraft:player")
            )
        except Exception as e:
            try:
                self.logger.error(f"[ARCAchievement] on_player_death error: {e}")
            except Exception:
                pass

    @event_handler
    def on_block_break(self, event: BlockBreakEvent):
        if self.achievement_system is None:
            return
        try:
            if getattr(event, "is_cancelled", False):
                return
            player = getattr(event, "player", None)
            block = getattr(event, "block", None)
            if player is None or block is None:
                return
            block_id = getattr(block, "type", None) or getattr(block, "type_id", None) or getattr(block, "identifier", None) or ""
            if block_id:
                bid = normalize_entity_type_id(str(block_id))
                self._schedule_activity_check(
                    lambda p=player, b=bid: self.achievement_system.record_block_break(p, b)
                )
        except Exception:
            pass

    @event_handler
    def on_block_place(self, event: BlockPlaceEvent):
        if self.achievement_system is None:
            return
        try:
            if getattr(event, "is_cancelled", False):
                return
            player = getattr(event, "player", None)
            if player is None:
                return
            placed = getattr(event, "block_placed", None) or getattr(event, "block", None)
            block_id = ""
            if placed is not None:
                block_id = (
                    getattr(placed, "type", None)
                    or getattr(placed, "identifier", None)
                    or ""
                )
            if block_id:
                bid = normalize_entity_type_id(str(block_id))
                self._schedule_activity_check(
                    lambda p=player, b=bid: self.achievement_system.record_block_place(p, b)
                )
        except Exception:
            pass


    def _announce_achievement_unlock(self, player: Player, achievement_name: str, unlock_title: str) -> None:
        """
        成就解锁全服通告 + QQ 群/跨服广播（通过 qqsync）。

        文案：
        玩家[头衔]名字解锁了成就【成就名】，获得头衔奖励【奖励头衔】
        其中【成就名】与【奖励头衔】使用“奖励头衔”的稀有色。
        """
        try:
            achievement_name = str(achievement_name or "").strip()
            unlock_title = str(unlock_title or "").strip()
            if not player or not achievement_name or not unlock_title:
                return

            player_display = self._format_player_broadcast_display(player)
            rarity_color = self._get_title_rarity_color(unlock_title)
            colored_achievement = f"{rarity_color}【{achievement_name}】§r"
            colored_title = f"{rarity_color}【{unlock_title}】§r"
            msg = f"玩家{player_display}解锁了成就{colored_achievement}，获得头衔奖励{colored_title}"

            # 游戏内全服广播
            try:
                self.server.broadcast_message(msg)
            except Exception:
                # 兼容部分端：broadcast_message 不存在则退化为遍历在线玩家
                for p in getattr(self.server, "online_players", []) or []:
                    try:
                        p.send_message(msg)
                    except Exception:
                        pass

            # QQ 群/跨服广播：交给 qqsync（多服部署时由 qqsync/机器人实现跨服同步）
            try:
                equipped = self._get_equipped_title(player)
                display_name = self._format_player_display_label(
                    getattr(player, "name", "") or "", equipped, str(player.xuid)
                )
                self._notify_qqsync("custom", display_name, getattr(player, "name", "") or "", msg)
            except Exception:
                pass
        except Exception as e:
            try:
                self.logger.error(f"[ARCAchievement]Announce achievement unlock error: {e}")
            except Exception:
                pass

    def show_my_achievements_hub(self, player: Player):
        """我的成就：已解锁 / 未解锁。"""
        panel = ActionForm(
            title=self.language_manager.GetText('MY_ACHIEVEMENTS_HUB_TITLE'),
            content=self.language_manager.GetText('MY_ACHIEVEMENTS_HUB_CONTENT'),
            on_close=None,
        )
        panel.add_button(
            self.language_manager.GetText('MY_ACHIEVEMENTS_UNLOCKED_LIST_BUTTON'),
            on_click=self.show_my_achievements_unlocked_list,
        )
        panel.add_button(
            self.language_manager.GetText('MY_ACHIEVEMENTS_LOCKED_LIST_BUTTON'),
            on_click=self.show_my_achievements_locked_list,
        )
        panel.add_button(
            self.language_manager.GetText('RETURN_BUTTON_TEXT'),
            on_click=self.return_to_my_info,
        )
        player.send_form(panel)

    def show_my_achievements_unlocked_list(self, player: Player):
        rows = self.achievement_system.list_unlocked_achievements_for_player_ui(str(player.xuid))
        panel = ActionForm(
            title=self.language_manager.GetText('MY_ACHIEVEMENTS_UNLOCKED_TITLE'),
            content=self.language_manager.GetText('MY_ACHIEVEMENTS_UNLOCKED_CONTENT'),
            on_close=None,
        )
        for achievement_row in rows:
            unlock_title = str(achievement_row.get("unlock_title") or "").strip()
            name = str(achievement_row.get("name") or unlock_title).strip()
            enabled = bool(achievement_row.get("enabled", True))
            if_hidden = bool(achievement_row.get("if_hidden", False))
            hidden_tag = self.language_manager.GetText('MY_ACHIEVEMENTS_TAG_HIDDEN') if if_hidden else ""
            status = self.language_manager.GetText('MY_ACHIEVEMENTS_STATUS_UNLOCKED')
            disabled_tag = "" if enabled else self.language_manager.GetText('MY_ACHIEVEMENTS_TAG_DISABLED')
            label = self.language_manager.GetText('MY_ACHIEVEMENTS_BUTTON_LABEL').format(
                name,
                unlock_title,
                hidden_tag + disabled_tag + status,
            )
            panel.add_button(
                label,
                on_click=lambda p, ut=unlock_title: self.show_my_achievement_detail(p, ut, "unlocked"),
            )
        panel.add_button(
            self.language_manager.GetText('RETURN_BUTTON_TEXT'),
            on_click=self.show_my_achievements_hub,
        )
        player.send_form(panel)

    def show_my_achievements_locked_list(self, player: Player):
        rows = self.achievement_system.list_locked_achievements_for_player_ui(str(player.xuid))
        panel = ActionForm(
            title=self.language_manager.GetText('MY_ACHIEVEMENTS_LOCKED_TITLE'),
            content=self.language_manager.GetText('MY_ACHIEVEMENTS_LOCKED_CONTENT'),
            on_close=None,
        )
        for achievement_row in rows:
            unlock_title = str(achievement_row.get("unlock_title") or "").strip()
            name = str(achievement_row.get("name") or unlock_title).strip()
            enabled = bool(achievement_row.get("enabled", True))
            status = self.language_manager.GetText('MY_ACHIEVEMENTS_STATUS_LOCKED')
            disabled_tag = "" if enabled else self.language_manager.GetText('MY_ACHIEVEMENTS_TAG_DISABLED')
            label = self.language_manager.GetText('MY_ACHIEVEMENTS_BUTTON_LABEL').format(
                name,
                unlock_title,
                disabled_tag + status,
            )
            panel.add_button(
                label,
                on_click=lambda p, ut=unlock_title: self.show_my_achievement_detail(p, ut, "locked"),
            )
        panel.add_button(
            self.language_manager.GetText('RETURN_BUTTON_TEXT'),
            on_click=self.show_my_achievements_hub,
        )
        player.send_form(panel)

    def _achievement_entity_label_for_player(self, entity_type_id: str) -> str:
        raw = str(entity_type_id or "").strip()
        if raw == "*":
            return self.language_manager.GetText('ACHIEVEMENT_ENTITY_ANY_LABEL')
        return self._entity_display_name(raw)

    def _format_player_achievement_condition_line(self, condition_data: dict) -> str:
        if not isinstance(condition_data, dict):
            return ""
        achievement_system = self.achievement_system
        ct = str(condition_data.get("condition_type") or condition_data.get("type") or "").strip()
        try:
            req = int(condition_data.get("required_count") or 0)
        except (TypeError, ValueError):
            req = 0
        if ct == achievement_system.condition_type_kill_entity_sum:
            target_ids = achievement_system._normalize_target_ids_list(condition_data.get("target_ids"))
            labels = [self._achievement_entity_label_for_player(x) for x in target_ids if str(x).strip()]
            sep = self.language_manager.GetText('ACHIEVEMENT_CONDITION_NAME_LIST_SEP')
            joined = sep.join(labels)
            return self.language_manager.GetText('ACHIEVEMENT_CONDITION_KILL_SUM').format(req, joined)
        if ct == achievement_system.condition_type_kill_entity:
            tid = str(condition_data.get("target_id") or "").strip()
            if tid == "*":
                return self.language_manager.GetText('ACHIEVEMENT_CONDITION_KILL_ANY').format(req)
            label = self._achievement_entity_label_for_player(tid)
            return self.language_manager.GetText('ACHIEVEMENT_CONDITION_KILL_ONE').format(label, req)
        if ct == achievement_system.condition_type_break_block_sum:
            target_ids = achievement_system._normalize_target_ids_list(condition_data.get("target_ids"))
            joined = "、".join(target_ids)
            return f"累计破坏下列方块合计不少于 {req} 次：{joined}"
        if ct == achievement_system.condition_type_break_block:
            tid = str(condition_data.get("target_id") or "").strip()
            if tid == "*":
                return f"累计破坏任意方块不少于 {req} 次。"
            return f"累计破坏 {tid} 不少于 {req} 次。"
        if ct == achievement_system.condition_type_place_block_sum:
            target_ids = achievement_system._normalize_target_ids_list(condition_data.get("target_ids"))
            joined = "、".join(target_ids)
            return f"累计放置下列方块合计不少于 {req} 次：{joined}"
        if ct == achievement_system.condition_type_place_block:
            tid = str(condition_data.get("target_id") or "").strip()
            if tid == "*":
                return f"累计放置任意方块不少于 {req} 次。"
            return f"累计放置 {tid} 不少于 {req} 次。"
        return self.language_manager.GetText('ACHIEVEMENT_CONDITION_UNKNOWN').format(ct, req)

    def _build_my_achievement_detail_body(
        self,
        achievement_data: dict,
        is_unlocked: bool,
    ) -> str:
        name = str(achievement_data.get("name") or "").strip()
        unlock_title = str(achievement_data.get("unlock_title") or "").strip()
        enabled = bool(achievement_data.get("enabled", True))
        if_hidden = bool(achievement_data.get("if_hidden", False))
        lines = []
        lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_NAME').format(name))
        lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_UNLOCK_TITLE').format(unlock_title))
        if if_hidden:
            lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_HIDDEN_TAG'))
        st_key = (
            'ACHIEVEMENT_DETAIL_STATUS_UNLOCKED_LINE'
            if is_unlocked
            else 'ACHIEVEMENT_DETAIL_STATUS_LOCKED_LINE'
        )
        lines.append(self.language_manager.GetText(st_key))
        if not enabled:
            lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_DISABLED_LINE'))
        lines.append("")
        lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_CONDITIONS_HEADER'))
        cond_list = achievement_data.get("conditions") or []
        if not cond_list:
            lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_NO_CONDITIONS'))
        else:
            idx = 1
            for cond in cond_list:
                line = self._format_player_achievement_condition_line(cond)
                lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_CONDITION_BULLET').format(idx, line))
                idx += 1
        defn_rarity = str(achievement_data.get("rarity") or "").strip()
        desc = str(achievement_data.get("description") or "").strip()
        reward_money = achievement_data.get("reward_money")
        reward_items = achievement_data.get("reward_items") or []
        if defn_rarity:
            lines.append("")
            lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_RARITY').format(defn_rarity))
        if desc:
            lines.append(self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_DESC').format(desc))
        try:
            rm = float(reward_money or 0)
            if rm > 0:
                lines.append(
                    self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_REWARD_MONEY').format(
                        self._format_money_display(rm)
                    )
                )
        except (TypeError, ValueError):
            pass
        item_bits = []
        for item in reward_items if isinstance(reward_items, list) else []:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("item_name") or item.get("id") or "").strip()
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if item_name and count > 0:
                item_bits.append(f"{item_name}×{count}")
        if item_bits:
            lines.append(
                self.language_manager.GetText('ACHIEVEMENT_DETAIL_LINE_REWARD_ITEMS').format(
                    "、".join(item_bits)
                )
            )
        return "\n".join(lines)

    def show_my_achievement_detail(self, player: Player, unlock_title: str, return_mode: str):
        achievement_row = self.achievement_system.get_achievement(unlock_title)
        if not achievement_row:
            player.send_message(self.language_manager.GetText('MY_ACHIEVEMENT_NOT_FOUND'))
            if return_mode == "locked":
                return self.show_my_achievements_locked_list(player)
            return self.show_my_achievements_unlocked_list(player)

        is_unlocked = self.achievement_system.player_has_unlocked_title(str(player.xuid), unlock_title)
        body = self._build_my_achievement_detail_body(achievement_row, is_unlocked)
        back_cb = (
            self.show_my_achievements_locked_list
            if return_mode == "locked"
            else self.show_my_achievements_unlocked_list
        )
        panel = ActionForm(
            title=self.language_manager.GetText('MY_ACHIEVEMENT_DETAIL_TITLE').format(
                str(achievement_row.get("name") or unlock_title).strip()
            ),
            content=body,
            on_close=None,
        )
        panel.add_button(self.language_manager.GetText('RETURN_BUTTON_TEXT'), on_click=back_cb)
        player.send_form(panel)

    def show_op_achievement_manage_panel(self, player: Player):
        """OP 成就管理：列表 / 创建 / 返回。"""
        panel = ActionForm(
            title=self.language_manager.GetText("OP_ACHIEVEMENT_PANEL_TITLE"),
            content=self.language_manager.GetText("OP_ACHIEVEMENT_PANEL_CONTENT"),
            on_close=None,
        )
        panel.add_button(self.language_manager.GetText("OP_ACHIEVEMENT_CREATE_BUTTON"),
                         on_click=self.show_op_achievement_create_panel)
        panel.add_button(self.language_manager.GetText("OP_ACHIEVEMENT_LIST_BUTTON"),
                         on_click=self.show_op_achievement_list_panel)
        panel.add_button(self.language_manager.GetText("OP_ACHIEVEMENT_APPLY_DEFAULT_BUTTON"),
                         on_click=self._do_op_apply_default_kill_achievements)
        panel.add_button(self.language_manager.GetText('RETURN_BUTTON_TEXT'),
                         on_click=self.return_to_op_main)
        player.send_form(panel)

    def _do_op_apply_default_kill_achievements(self, player: Player):
        bundle_size = self.achievement_system.get_horror_kill_bundle_size()
        ok = self.achievement_system.apply_horror_kill_achievement_bundle(self._title_bridge)
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_APPLY_DEFAULT_DONE").format(bundle_size))
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
        self.show_op_achievement_manage_panel(player)

    def show_op_achievement_list_panel(self, player: Player):
        achievement_rows = self.achievement_system.list_achievements()
        if not achievement_rows:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_EMPTY_HINT"))
            return self.show_op_achievement_manage_panel(player)

        panel = ActionForm(
            title=self.language_manager.GetText("OP_ACHIEVEMENT_LIST_TITLE"),
            content=self.language_manager.GetText("OP_ACHIEVEMENT_LIST_CONTENT"),
            on_close=None,
        )
        for achievement_row in achievement_rows:
            name = str(achievement_row.get("name") or "").strip()
            unlock_title = str(achievement_row.get("unlock_title") or "").strip()
            enabled = int(achievement_row.get("enabled") or 0) == 1
            if_hidden = bool(achievement_row.get("if_hidden", False))
            condition_count = len(self.achievement_system.list_conditions(unlock_title))
            status = "§aON§r" if enabled else "§cOFF§r"
            hid = self.language_manager.GetText('OP_ACHIEVEMENT_HIDDEN_TAG') if if_hidden else ""
            label = f"{hid}{status} {name}\n头衔: {unlock_title} | 条件数: {condition_count} | 逻辑: all"
            panel.add_button(
                label,
                on_click=lambda p, ut=unlock_title: self.show_op_achievement_edit_panel(p, ut),
            )
        panel.add_button(self.language_manager.GetText('RETURN_BUTTON_TEXT'),
                         on_click=self.show_op_achievement_manage_panel)
        player.send_form(panel)

    @staticmethod
    def _parse_reward_items_text(text: str) -> list:
        """解析 '物品ID 数量; 物品ID 数量' 为 [{"item_name": id, "count": n}, ...]"""
        result = []
        for part in str(text or "").replace("；", ";").split(";"):
            tokens = [t for t in part.strip().split() if t]
            if not tokens:
                continue
            try:
                if len(tokens) >= 2:
                    result.append({"item_name": tokens[0], "count": int(tokens[1])})
                else:
                    result.append({"item_name": tokens[0], "count": 1})
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _format_reward_items_text(items) -> str:
        if not isinstance(items, list):
            return ""
        bits = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("item_name") or item.get("id") or "").strip()
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if item_name and count > 0:
                bits.append(f"{item_name} {count}")
        return "; ".join(bits)

    def show_op_achievement_create_panel(self, player: Player):
        """创建成就基础信息（含稀有度/介绍/奖励）。"""
        name_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_NAME"),
            placeholder="例如：僵尸杀手",
            default_value="",
        )
        title_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_UNLOCK_TITLE"),
            placeholder="例如：僵尸杀手",
            default_value="",
        )
        rarity_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_RARITY"),
            placeholder="普通/稀有/史诗/传奇/神话",
            default_value="普通",
        )
        desc_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_DESCRIPTION"),
            placeholder="头衔介绍（可空）",
            default_value="",
        )
        money_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REWARD_MONEY"),
            placeholder="0",
            default_value="0",
        )
        items_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REWARD_ITEMS"),
            placeholder=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REWARD_ITEMS_HINT"),
            default_value="",
        )
        enabled_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_ENABLED"),
            placeholder="1=启用 0=禁用",
            default_value="1",
        )
        hidden_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_IF_HIDDEN"),
            placeholder=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_IF_HIDDEN_HINT"),
            default_value="0",
        )
        form = ModalForm(
            title=self.language_manager.GetText("OP_ACHIEVEMENT_CREATE_TITLE"),
            controls=[
                name_input,
                title_input,
                rarity_input,
                desc_input,
                money_input,
                items_input,
                enabled_input,
                hidden_input,
            ],
            on_close=None,
            on_submit=self._do_op_achievement_create,
        )
        player.send_form(form)

    def _do_op_achievement_create(self, player: Player, json_str: str):
        try:
            data = json.loads(json_str)
        except Exception:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_manage_panel(player)

        if not data or len(data) < 2:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_manage_panel(player)

        name = str(data[0] or "").strip()
        unlock_title = str(data[1] or "").strip()
        rarity = str(data[2] or "普通").strip() if len(data) > 2 else "普通"
        description = str(data[3] or "").strip() if len(data) > 3 else ""
        try:
            reward_money = float(data[4]) if len(data) > 4 and str(data[4] or "").strip() else 0.0
        except (TypeError, ValueError):
            reward_money = 0.0
        reward_items = self._parse_reward_items_text(str(data[5]) if len(data) > 5 else "")
        enabled = True
        if len(data) > 6:
            enabled = str(data[6] or "1").strip() not in ["0", "false", "False", "off", "OFF"]
        if_hidden = False
        if len(data) > 7:
            if_hidden = str(data[7] or "0").strip() in ["1", "true", "True", "yes", "YES", "on", "ON"]

        ok = self.achievement_system.create_achievement(
            name=name,
            unlock_title=unlock_title,
            enabled=enabled,
            if_hidden=if_hidden,
            rarity=rarity,
            description=description,
            reward_money=reward_money,
            reward_items=reward_items,
        )
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_SUCCESS"))
            self.show_op_achievement_edit_panel(player, unlock_title)
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            self.show_op_achievement_manage_panel(player)

    def show_op_achievement_edit_panel(self, player: Player, unlock_title: str):
        achievement_row = self.achievement_system.get_achievement(unlock_title)
        if not achievement_row:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_NOT_FOUND"))
            return self.show_op_achievement_list_panel(player)

        name = str(achievement_row.get("name") or "").strip()
        current_unlock_title = str(achievement_row.get("unlock_title") or "").strip()
        enabled = int(achievement_row.get("enabled") or 0) == 1
        if_hidden = bool(achievement_row.get("if_hidden", False))
        rarity = str(achievement_row.get("rarity") or "普通").strip()
        reward_money = achievement_row.get("reward_money") or 0
        reward_items = achievement_row.get("reward_items") or []
        condition_rows = self.achievement_system.list_conditions(current_unlock_title)
        items_preview = self._format_reward_items_text(reward_items) or "无"

        panel = ActionForm(
            title=f"编辑成就: {name}",
            content=(
                f"头衔: {current_unlock_title}\n"
                f"稀有度: {rarity}\n"
                f"奖励存款: {reward_money}\n"
                f"奖励物品: {items_preview}\n"
                f"状态: {'启用' if enabled else '禁用'}\n"
                f"隐藏: {'是' if if_hidden else '否'}\n"
                "逻辑: all\n"
                f"条件数: {len(condition_rows)}\n"
                "说明: 全部条件满足后才会解锁；奖励由成就插件发放。"
            ),
            on_close=None,
        )
        panel.add_button(
            "编辑基础信息",
            on_click=lambda p, ut=current_unlock_title: self._show_op_achievement_edit_meta_modal(p, ut),
        )
        for condition_row in condition_rows:
            condition_id = int(condition_row.get("id") or 0)
            condition_type = str(condition_row.get("condition_type") or "").strip()
            target_id = str(condition_row.get("target_id") or "").strip()
            required_count = int(condition_row.get("required_count") or 0)
            if condition_type == self.achievement_system.condition_type_kill_entity_sum:
                ids_joined = ", ".join(condition_row.get("target_ids") or [])
                condition_text = f"击杀总和 [{ids_joined}] >= {required_count}"
            elif condition_type == self.achievement_system.condition_type_kill_entity:
                if target_id == "*":
                    condition_text = f"累计击杀任意生物 >= {required_count}"
                else:
                    condition_text = f"击杀 {target_id} >= {required_count}"
            elif condition_type == self.achievement_system.condition_type_break_block_sum:
                ids_joined = ", ".join(condition_row.get("target_ids") or [])
                condition_text = f"破坏总和 [{ids_joined}] >= {required_count}"
            elif condition_type == self.achievement_system.condition_type_break_block:
                if target_id == "*":
                    condition_text = f"累计破坏任意方块 >= {required_count}"
                else:
                    condition_text = f"破坏 {target_id} >= {required_count}"
            elif condition_type == self.achievement_system.condition_type_place_block_sum:
                ids_joined = ", ".join(condition_row.get("target_ids") or [])
                condition_text = f"放置总和 [{ids_joined}] >= {required_count}"
            elif condition_type == self.achievement_system.condition_type_place_block:
                if target_id == "*":
                    condition_text = f"累计放置任意方块 >= {required_count}"
                else:
                    condition_text = f"放置 {target_id} >= {required_count}"
            else:
                condition_text = f"{condition_type}:{target_id} >= {required_count}"
            panel.add_button(
                f"条件 #{condition_id}\n{condition_text}",
                on_click=lambda p, ut=current_unlock_title, c_id=condition_id: self.show_op_achievement_condition_panel(p, ut, c_id),
            )
        panel.add_button(
            "新增条件",
            on_click=lambda p, ut=current_unlock_title: self._show_op_achievement_create_condition_modal(p, ut),
        )
        toggle_text = self.language_manager.GetText("OP_ACHIEVEMENT_DISABLE_BUTTON") if enabled else self.language_manager.GetText("OP_ACHIEVEMENT_ENABLE_BUTTON")
        panel.add_button(
            toggle_text,
            on_click=lambda p, ut=current_unlock_title, en=enabled: self._do_op_achievement_toggle(p, ut, not en),
        )
        hidden_toggle = (
            self.language_manager.GetText("OP_ACHIEVEMENT_CLEAR_HIDDEN_BUTTON")
            if if_hidden
            else self.language_manager.GetText("OP_ACHIEVEMENT_SET_HIDDEN_BUTTON")
        )
        panel.add_button(
            hidden_toggle,
            on_click=lambda p, ut=current_unlock_title, h=if_hidden: self._do_op_achievement_toggle_hidden(
                p, ut, not h
            ),
        )
        panel.add_button(
            self.language_manager.GetText("OP_ACHIEVEMENT_DELETE_BUTTON"),
            on_click=lambda p, ut=current_unlock_title: self._do_op_achievement_delete(p, ut),
        )
        panel.add_button(self.language_manager.GetText('RETURN_BUTTON_TEXT'),
                         on_click=self.show_op_achievement_list_panel)
        player.send_form(panel)

    def _show_op_achievement_edit_meta_modal(self, player: Player, unlock_title: str):
        achievement_row = self.achievement_system.get_achievement(unlock_title)
        if not achievement_row:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_NOT_FOUND"))
            return self.show_op_achievement_list_panel(player)

        name_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_NAME"),
            placeholder="",
            default_value=str(achievement_row.get("name") or ""),
        )
        title_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_UNLOCK_TITLE"),
            placeholder="",
            default_value=str(achievement_row.get("unlock_title") or ""),
        )
        rarity_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_RARITY"),
            placeholder="普通/稀有/史诗/传奇/神话",
            default_value=str(achievement_row.get("rarity") or "普通"),
        )
        desc_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_DESCRIPTION"),
            placeholder="头衔介绍（可空）",
            default_value=str(achievement_row.get("description") or ""),
        )
        money_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REWARD_MONEY"),
            placeholder="0",
            default_value=str(achievement_row.get("reward_money") or 0),
        )
        items_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REWARD_ITEMS"),
            placeholder=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REWARD_ITEMS_HINT"),
            default_value=self._format_reward_items_text(achievement_row.get("reward_items")),
        )
        enabled_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_ENABLED"),
            placeholder="1=启用 0=禁用",
            default_value="1" if int(achievement_row.get("enabled") or 0) == 1 else "0",
        )
        hidden_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_IF_HIDDEN"),
            placeholder=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_IF_HIDDEN_HINT"),
            default_value="1" if bool(achievement_row.get("if_hidden", False)) else "0",
        )
        form = ModalForm(
            title=f"编辑成就信息: {unlock_title}",
            controls=[
                name_input,
                title_input,
                rarity_input,
                desc_input,
                money_input,
                items_input,
                enabled_input,
                hidden_input,
            ],
            on_close=None,
            on_submit=lambda p, json_str, ut=unlock_title: self._do_op_achievement_save_meta(p, json_str, ut),
        )
        player.send_form(form)

    def _do_op_achievement_save_meta(self, player: Player, json_str: str, old_unlock_title: str):
        try:
            data = json.loads(json_str)
        except Exception:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_edit_panel(player, old_unlock_title)

        if not data or len(data) < 2:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_edit_panel(player, old_unlock_title)

        name = str(data[0] or "").strip()
        new_unlock_title = str(data[1] or "").strip()
        rarity = str(data[2] or "普通").strip() if len(data) > 2 else "普通"
        description = str(data[3] or "").strip() if len(data) > 3 else ""
        try:
            reward_money = float(data[4]) if len(data) > 4 and str(data[4] or "").strip() else 0.0
        except (TypeError, ValueError):
            reward_money = 0.0
        reward_items = self._parse_reward_items_text(str(data[5]) if len(data) > 5 else "")
        enabled = True
        if len(data) > 6:
            enabled = str(data[6] or "1").strip() not in ["0", "false", "False", "off", "OFF"]
        if_hidden = False
        if len(data) > 7:
            if_hidden = str(data[7] or "0").strip() in ["1", "true", "True", "yes", "YES", "on", "ON"]

        ok = self.achievement_system.update_achievement(
            old_unlock_title=old_unlock_title,
            name=name,
            new_unlock_title=new_unlock_title,
            enabled=enabled,
            if_hidden=if_hidden,
            rarity=rarity,
            description=description,
            reward_money=reward_money,
            reward_items=reward_items,
        )
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_SUCCESS"))
            self.show_op_achievement_edit_panel(player, new_unlock_title)
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            self.show_op_achievement_edit_panel(player, old_unlock_title)

    def _show_op_achievement_create_condition_modal(self, player: Player, unlock_title: str):
        entity_input = TextInput(
            label="生物ID",
            placeholder="例如: minecraft:zombie 或 *",
            default_value="minecraft:zombie",
        )
        required_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REQUIRED"),
            placeholder="例如：100",
            default_value="100",
        )
        form = ModalForm(
            title=f"新增条件: {unlock_title}",
            controls=[entity_input, required_input],
            on_close=None,
            on_submit=lambda p, json_str, ut=unlock_title: self._do_op_achievement_create_condition(p, json_str, ut),
        )
        player.send_form(form)

    def _do_op_achievement_create_condition(self, player: Player, json_str: str, unlock_title: str):
        try:
            data = json.loads(json_str)
        except Exception:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_edit_panel(player, unlock_title)

        if not data or len(data) < 2:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_edit_panel(player, unlock_title)

        target_id = str(data[0] or "").strip()
        try:
            required_count_int = int(data[1])
        except Exception:
            required_count_int = 0

        ok = self.achievement_system.create_condition(
            unlock_title=unlock_title,
            condition_type=self.achievement_system.condition_type_kill_entity,
            target_id=target_id,
            required_count=required_count_int,
        )
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_SUCCESS"))
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
        self.show_op_achievement_edit_panel(player, unlock_title)

    def show_op_achievement_condition_panel(self, player: Player, unlock_title: str, condition_id: int):
        condition_row = self.achievement_system.get_condition(condition_id)
        if not condition_row:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_NOT_FOUND"))
            return self.show_op_achievement_edit_panel(player, unlock_title)

        target_id = str(condition_row.get("target_id") or "").strip()
        required_count = int(condition_row.get("required_count") or 0)
        condition_type = str(condition_row.get("condition_type") or "").strip()
        if condition_type == self.achievement_system.condition_type_kill_entity_sum:
            ids_joined = ", ".join(condition_row.get("target_ids") or [])
            condition_text = f"击杀总和 [{ids_joined}] >= {required_count}"
        elif target_id == "*":
            condition_text = f"累计击杀任意生物 >= {required_count}"
        else:
            condition_text = f"击杀 {target_id} >= {required_count}"

        panel = ActionForm(
            title=f"条件 #{condition_id}",
            content=condition_text,
            on_close=None,
        )
        panel.add_button(
            "编辑条件",
            on_click=lambda p, ut=unlock_title, c_id=condition_id: self._show_op_achievement_edit_condition_modal(p, ut, c_id),
        )
        panel.add_button(
            self.language_manager.GetText("OP_ACHIEVEMENT_DELETE_BUTTON"),
            on_click=lambda p, ut=unlock_title, c_id=condition_id: self._do_op_achievement_delete_condition(p, ut, c_id),
        )
        panel.add_button(
            self.language_manager.GetText('RETURN_BUTTON_TEXT'),
            on_click=lambda p, ut=unlock_title: self.show_op_achievement_edit_panel(p, ut),
        )
        player.send_form(panel)

    def _show_op_achievement_edit_condition_modal(self, player: Player, unlock_title: str, condition_id: int):
        condition_row = self.achievement_system.get_condition(condition_id)
        if not condition_row:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_NOT_FOUND"))
            return self.show_op_achievement_edit_panel(player, unlock_title)

        condition_type = str(condition_row.get("condition_type") or "").strip()
        if condition_type == self.achievement_system.condition_type_kill_entity_sum:
            default_ids = ", ".join(condition_row.get("target_ids") or [])
            entity_input = TextInput(
                label="生物ID（英文逗号分隔，击杀数相加）",
                placeholder="minecraft:zombie, minecraft:skeleton",
                default_value=default_ids,
            )
        else:
            entity_input = TextInput(
                label="生物ID",
                placeholder="例如: minecraft:zombie 或 *",
                default_value=str(condition_row.get("target_id") or ""),
            )
        required_input = TextInput(
            label=self.language_manager.GetText("OP_ACHIEVEMENT_FIELD_REQUIRED"),
            placeholder="",
            default_value=str(int(condition_row.get("required_count") or 0)),
        )
        form = ModalForm(
            title=f"编辑条件 #{condition_id}",
            controls=[entity_input, required_input],
            on_close=None,
            on_submit=lambda p, json_str, ut=unlock_title, c_id=condition_id: self._do_op_achievement_save_condition(p, json_str, ut, c_id),
        )
        player.send_form(form)

    def _do_op_achievement_save_condition(self, player: Player, json_str: str, unlock_title: str, condition_id: int):
        condition_row = self.achievement_system.get_condition(condition_id)
        try:
            data = json.loads(json_str)
        except Exception:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_condition_panel(player, unlock_title, condition_id)

        if not data or len(data) < 2:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            return self.show_op_achievement_condition_panel(player, unlock_title, condition_id)

        try:
            required_count_int = int(data[1])
        except Exception:
            required_count_int = 0

        condition_type = str((condition_row or {}).get("condition_type") or "").strip()
        if condition_type == self.achievement_system.condition_type_kill_entity_sum:
            ids_raw = str(data[0] or "")
            target_ids_list = [x.strip() for x in ids_raw.split(",") if x.strip()]
            ok = self.achievement_system.update_condition(
                condition_id=condition_id,
                condition_type=self.achievement_system.condition_type_kill_entity_sum,
                target_id="",
                required_count=required_count_int,
                target_ids=target_ids_list,
            )
        else:
            target_id = str(data[0] or "").strip()
            ok = self.achievement_system.update_condition(
                condition_id=condition_id,
                condition_type=self.achievement_system.condition_type_kill_entity,
                target_id=target_id,
                required_count=required_count_int,
            )
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_SUCCESS"))
            self.show_op_achievement_condition_panel(player, unlock_title, condition_id)
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
            self.show_op_achievement_condition_panel(player, unlock_title, condition_id)

    def _do_op_achievement_delete_condition(self, player: Player, unlock_title: str, condition_id: int):
        ok = self.achievement_system.delete_condition(int(condition_id))
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_DELETE_SUCCESS"))
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_DELETE_FAIL"))
        self.show_op_achievement_edit_panel(player, unlock_title)

    def _do_op_achievement_toggle(self, player: Player, unlock_title: str, enabled: bool):
        ok = self.achievement_system.set_achievement_enabled(unlock_title, bool(enabled))
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_SUCCESS"))
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
        self.show_op_achievement_edit_panel(player, unlock_title)

    def _do_op_achievement_toggle_hidden(self, player: Player, unlock_title: str, if_hidden: bool):
        ok = self.achievement_system.set_achievement_if_hidden(unlock_title, bool(if_hidden))
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_SUCCESS"))
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_SAVE_FAIL"))
        self.show_op_achievement_edit_panel(player, unlock_title)

    def _do_op_achievement_delete(self, player: Player, unlock_title: str):
        ok = self.achievement_system.delete_achievement(unlock_title)
        if ok:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_DELETE_SUCCESS"))
        else:
            player.send_message(self.language_manager.GetText("OP_ACHIEVEMENT_DELETE_FAIL"))
        self.show_op_achievement_list_panel(player)

