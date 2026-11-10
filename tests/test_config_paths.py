"""配置路径解析：相对路径必须落在仓库根，不能被启动目录带偏。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trina.config import PACKAGED_CONFIG_PATH, PROJECT_ROOT, TrinaConfig, _resolve

CONFIG = PROJECT_ROOT / "configs" / "trina.yaml"


class ConfigPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_relative_path_anchors_to_project_root(self) -> None:
        self.assertEqual(_resolve("logs"), PROJECT_ROOT / "logs")
        self.assertEqual(_resolve("runs/20260101/a"), PROJECT_ROOT / "runs/20260101/a")

    def test_absolute_path_is_untouched(self) -> None:
        absolute = str(Path(self._tmp.name) / "out")
        self.assertEqual(_resolve(absolute), Path(absolute))

    def test_runtime_dirs_ignore_foreign_cwd(self) -> None:
        cfg = TrinaConfig.load(path=CONFIG)
        self.assertEqual(cfg.audit_dir, PROJECT_ROOT / cfg.audit.dir)
        self.assertEqual(cfg.report_dir, PROJECT_ROOT / cfg.report.output_dir)
        self.assertEqual(cfg.workspace_dir, PROJECT_ROOT / cfg.run.workspace)

    def test_override_accepts_absolute_dir(self) -> None:
        target = Path(self._tmp.name) / "custom-logs"
        cfg = TrinaConfig.load(path=CONFIG, overrides={"audit.dir": str(target)})
        self.assertEqual(cfg.audit_dir, target)

    def test_packaged_config_writes_to_user_data_not_site_packages(self) -> None:
        data_root = Path(self._tmp.name) / "trina-home"
        cfg = TrinaConfig(path=PACKAGED_CONFIG_PATH)
        with patch.dict(os.environ, {"TRINA_HOME": str(data_root)}):
            self.assertEqual(cfg.runtime_root, data_root)
            self.assertEqual(cfg.audit_dir, data_root / "logs")
            self.assertNotIn("site-packages", str(cfg.audit_dir).lower())

    def test_explicit_config_owns_relative_runtime_paths(self) -> None:
        project = Path(self._tmp.name) / "project"
        cfg = TrinaConfig(path=project / "configs" / "custom.yaml")
        self.assertEqual(cfg.runtime_root, project)
        self.assertEqual(cfg.workspace_dir, project / "runs")


if __name__ == "__main__":
    unittest.main()
