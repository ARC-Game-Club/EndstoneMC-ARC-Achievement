# EndStone ARC Achievement / 弧光成就

[![Codacy Grade](https://app.codacy.com/project/badge/Grade/4c54f9ddc5e54246aea130507b71321e)](https://app.codacy.com/gh/ARC-Minecraft/EndstoneMC-ARC-Achievement/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)
[![版本](https://img.shields.io/badge/版本-0.1.6-blue.svg)](https://github.com/ARC-Minecraft/EndstoneMC-ARC-Achievement)
[![EndStone](https://img.shields.io/badge/EndStone-0.10+-green.svg)](https://github.com/EndstoneMC/endstone)
[![依赖](https://img.shields.io/badge/依赖-arc__core-orange.svg)](https://github.com/ARC-Minecraft/EndstoneMC-ARC-Core-Plugin)

弧光系列成就插件：可配置击杀类成就、玩家进度面板与 OP 管理界面。头衔注册/解锁走 **[弧光核心](https://github.com/ARC-Minecraft/EndstoneMC-ARC-Core-Plugin)**（`arc_core`）；金钱/物品奖励由本插件配置并发放。

## 命名约定

| 项 | 值 |
|---|---|
| 包名 | `endstone_arc_achievement` |
| Plugin id | `arc_achievement` |
| 数据目录 | `plugins/ARCAchievement/` |
| 依赖 | 必须安装并启用 `arc_core`（建议 ≥ 0.9.11，需活动统计 API） |

## 功能特性

- **击杀成就**：`kill_entity`（单种生物，`*` 表示任意生物累计）、`kill_entity_sum`（多种生物击杀数相加）
- **方块成就**：`break_block` / `place_block`（及 `_sum`）；计数由弧光核心维护
- **JSON 配置**：定义保存在 `plugins/ARCAchievement/achievements.json`（含稀有度、介绍、奖励）
- **进度查询**：经 `arc_core` 只读 API（`api_get_player_stat` / `api_get_player_kill_count` 等）读取 `player_activity_stats`；**本插件不写核心库**
- **解锁标记**：本地 `plugins/ARCAchievement/achievement.db`
- **玩家面板**：已解锁 / 未解锁列表与条件说明；隐藏成就未达成前不展示
- **OP 面板**：新建成就、编辑条件与奖励、启用/隐藏/删除；一键写入内置击杀成就包（含恐怖服包）
- **解锁流程**：按「头衔名+稀有度」查核心是否已注册 → 未注册则 `api_ensure_title_definition` → `api_unlock_title(..., rarity=)` → 本插件发放金钱/物品 + toast；全服通告 + QQ Sync `custom` 事件
- **菜单集成**：检测到本插件时，弧光核心「我的信息 → 我的成就」与 OP「成就管理」自动出现

## 安装

1. 确保已安装 **弧光核心** `endstone_arc_core`（`arc_core`，建议 ≥ 0.9.7）
2. 将本插件 `.whl` 放入 EndStone 服务器的 `plugins/` 目录（与其它弧光插件同级）
3. 重启服务器；首次启动会创建 `plugins/ARCAchievement/`

### 从旧版核心迁移

若服务器上仍有 `plugins/ARCCore/achievements.json`，首次启用本插件时会**自动复制**到 `plugins/ARCAchievement/achievements.json`（仅当目标文件尚不存在）。击杀统计表仍在核心数据库中，无需手工迁库。

可选：将仓库内 `dist/ARCAchievement/ZH-CN.txt` 复制到 `plugins/ARCAchievement/`（插件也会尝试从包内自带语言文件初始化）。

启用时若成就尚未配置奖励字段，会尝试从核心已有头衔定义回填一次。

### 本地构建

```bash
pip install build
python -m build --wheel
# 输出：dist/endstone_arc_achievement-<version>-py2.py3-none-any.whl
```

## 命令

| 命令 | 权限 | 说明 |
|------|------|------|
| `/ach` | 所有玩家 | 打开「我的成就」 |
| `/achop` | OP | 打开成就管理面板 |

## 配置文件

运行时目录：`plugins/ARCAchievement/`

| 文件 | 说明 |
|------|------|
| `achievements.json` | 成就定义（名称、解锁头衔、稀有度/介绍、奖励、条件列表、启用/隐藏等） |
| `ZH-CN.txt` | 界面文案（`KEY=VALUE`） |

成就 JSON 结构概要：

```json
{
  "version": 1,
  "achievements": [
    {
      "name": "赶尸人",
      "unlock_title": "赶尸人",
      "rarity": "普通",
      "description": "",
      "reward_money": 2000,
      "reward_items": [],
      "enabled": true,
      "if_hidden": false,
      "logic": "all",
      "conditions": [
        {
          "id": 1,
          "type": "kill_entity",
          "condition_type": "kill_entity",
          "target_id": "minecraft:zombie",
          "required_count": 100
        }
      ]
    }
  ]
}
```

金钱/物品奖励在本插件配置并发放；核心只负责头衔注册与解锁。

## 依赖的核心 API

通过 `server.get_plugin("arc_core")` 调用：

- `api_has_title_definition(title, rarity)` / `api_ensure_title_definition` / `api_get_title_definition`（按名称+稀有度）
- `api_unlock_title(player, title, rarity=...)` / `api_has_unlocked_title(..., rarity=...)`
- `increase_player_money` / `api_give_player_items`
- 复用 `arc_core.database_manager` 读写 `player_achievement_stats`

未找到 `arc_core` 时，本插件会打错误日志并禁用成就逻辑。

## 与弧光核心的关系

| 能力 | 所在插件 |
|------|----------|
| 头衔定义、解锁、聊天展示 | `arc_core` |
| 成就条件、击杀统计、成就奖励、成就 UI | `arc_achievement` |
| 击杀赏金（`kill_reward.txt`） | `arc_core`（与成就独立） |

## 许可证

见 [LICENSE](LICENSE)。
