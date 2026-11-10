"""模型代号显示：API 模型 ID 必须保持原样，展示层换成人类可读代号。"""

import unittest

from trina.model_names import base_name, display_model, effort_label


class ModelNameTest(unittest.TestCase):
    def test_deepseek_flash_max(self) -> None:
        self.assertEqual(display_model("deepseek-flash", "max"), "DeepSeek V4.1 Flash Max")

    def test_provider_prefix_is_stripped(self) -> None:
        self.assertEqual(display_model("deepseek/deepseek-v4-flash", "high"), "DeepSeek V4 Flash High")

    def test_default_effort_has_no_suffix(self) -> None:
        self.assertEqual(display_model("deepseek-flash", "default"), "DeepSeek V4.1 Flash")

    def test_unknown_model_keeps_its_id(self) -> None:
        self.assertEqual(display_model("my-local-model", "default"), "my-local-model")

    def test_empty_model_is_labelled_not_blank(self) -> None:
        self.assertEqual(display_model("", "max"), "未配置")
        self.assertEqual(base_name("  "), "未配置")

    def test_effort_label_is_case_insensitive_and_unknown_is_empty(self) -> None:
        self.assertEqual(effort_label("MAX"), "Max")
        self.assertEqual(effort_label("nonsense"), "")


if __name__ == "__main__":
    unittest.main()
