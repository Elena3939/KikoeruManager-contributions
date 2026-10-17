"""文件名标签式密码（解压码1021）提取单测。"""

import pytest

from app.core.password_utils import extract_labeled_password


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("解压码1021", "1021"),
        ("解压码1021.zip", "1021"),
        ("解压码：abc123.rar", "abc123"),
        ("解压码: abc123.7z", "abc123"),
        ("解压码=8888", "8888"),
        ("压缩包密码 7777", "7777"),
        ("解壓碼：8888", "8888"),
        ("RJ01667840(解压码1021)", "1021"),
        ("RJ01667840(解压码1021).zip", "1021"),
        ("[RJ01234567] 解压密码：xyz_789", "xyz_789"),
        ("解压码1021.part1.rar", "1021"),
        ("解压码1021.7z.001", "1021"),
        ("archive password: hello123", "hello123"),
        ("unzip password = qwe456", "qwe456"),
        # 连续标签只在第一个值处截断
        ("解压码1021解压码2024", "1021"),
    ],
)
def test_extracts_labeled_password(text, expected):
    assert extract_labeled_password(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "解压码",
        "解压码.zip",  # 只有扩展名，不能把 zip 当密码
        "解压码.7z",
        "解压码 无",
        "普通文件名 RJ01667840.zip",
        "RJ01607556(0822).7z",  # 括号里是密码但没标签 → 不猜
    ],
)
def test_returns_empty_when_no_usable_label(text):
    assert extract_labeled_password(text) == ""


def test_rejects_overlong_value():
    assert extract_labeled_password("解压码" + "a" * 200) == ""


def test_accepts_unicode_password():
    assert extract_labeled_password("解压码：密码一二三.zip") == "密码一二三"


@pytest.mark.parametrize(
    "text",
    [
        "无解压码说明.txt",
        "没有解压码.zip",
        "不含解压码的包.7z",
        "解压码说明.txt",
        "解压码提示.txt",
    ],
)
def test_negated_label_is_not_a_password(text):
    """「无解压码说明」这类否定/说明性文件名不得把后面的普通词当密码。"""
    assert extract_labeled_password(text) == ""
