from pathlib import Path

MAIN_PATH = "plugins/ARCAchievement"


class LanguageManager:
    language_dict = {}

    def __init__(self, default_language_code):
        self.language_code = default_language_code.upper()
        if self.language_code not in LanguageManager.language_dict:
            LanguageManager.language_dict[self.language_code] = {}
        self.language_file_path = Path(MAIN_PATH) / f"{self.language_code}.txt"
        self._load_language_file()

    def _load_language_file(self):
        self.language_file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.language_file_path.exists():
            # 首次运行：优先包内自带 ZH-CN.txt，其次仓库 dist 模板
            candidates = [
                Path(__file__).resolve().parent / f"{self.language_code}.txt",
                Path(__file__).resolve().parents[2] / "dist" / "ARCAchievement" / f"{self.language_code}.txt",
            ]
            copied = False
            for bundled in candidates:
                if bundled.exists():
                    self.language_file_path.write_text(
                        bundled.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                    copied = True
                    break
            if not copied:
                self.language_file_path.touch()
        with self.language_file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and "=" in line:
                    key, value = line.split("=", 1)
                    LanguageManager.language_dict[self.language_code][key.strip()] = value.strip()

    def GetText(self, key, lang_code=None):
        target_lang = (lang_code or self.language_code).upper()
        if target_lang not in LanguageManager.language_dict:
            LanguageManager(target_lang)
        if key not in LanguageManager.language_dict[target_lang]:
            target_file_path = Path(MAIN_PATH) / f"{target_lang}.txt"
            with target_file_path.open("a", encoding="utf-8") as f:
                f.write(f"\n{key}=")
            LanguageManager.language_dict[target_lang][key] = ""
        if not LanguageManager.language_dict[target_lang][key]:
            print(f"[ARCAchievement] Key {key} not found in language file {target_lang}.txt.")
            return ""
        return LanguageManager.language_dict[target_lang][key].replace("\\n", "\n")

    def ReloadCurrentLanguage(self):
        if self.language_code in LanguageManager.language_dict:
            LanguageManager.language_dict[self.language_code].clear()
        self._load_language_file()
