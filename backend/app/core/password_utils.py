import re
from typing import Optional

_ZERO_WIDTH_TRANSLATION = {
    ord("\u200b"): None,  # zero width space
    ord("\u200c"): None,  # zero width non-joiner
    ord("\u200d"): None,  # zero width joiner
    ord("\ufeff"): None,  # bom / zero width no-break space
    ord("\u2060"): None,  # word joiner
}

_RJCODE_PATTERN = re.compile(r"([RVB]J)\s*[-_.]?\s*(\d{6,8})(?!\d)", re.IGNORECASE)


def normalize_optional_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def normalize_password_value(value: Optional[str]) -> str:
    normalized = str(value or "")
    normalized = normalized.translate(_ZERO_WIDTH_TRANSLATION)
    normalized = normalized.strip()
    return normalized


def normalize_rjcode_value(value: Optional[str]) -> Optional[str]:
    normalized = normalize_optional_text(value)
    if not normalized:
        return None
    match = _RJCODE_PATTERN.search(normalized)
    if match:
        return f"RJ{match.group(2)}"
    return normalized.upper()


def normalize_filename_value(value: Optional[str]) -> Optional[str]:
    return normalize_optional_text(value)


# ---------------------------------------------------------------------------
# 文件名标签式密码（解压码1021 / 解压码：abc123.zip / 解压密码 abc.rar）
#
# 背景：国内网盘最常见的形态是把密码直接写进文件名，既不落在「规范输入行的
# 解压码段」，也匹配不上 ``{name}({password})`` 模板，导致外层包与嵌套包都拿
# 不到密码。这里只做「从一段文本里摘标签值」这一件事，供预览命名与解压候选
# 两条链路复用。
# ---------------------------------------------------------------------------

_LABELED_PASSWORD_RE = re.compile(
    r"(?:解压密码|解壓密碼|解压码|解壓碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼|"
    r"extract\s*password|archive\s*password|unzip\s*password)"
    r"\s*(?:[:：=]|是|为|為)?\s*([^\s,，。;；、|｜/\\]+)",
    re.IGNORECASE,
)

# 只匹配标签本身，用于在捕获值里切断下一个标签（``解压码1021解压码2024``）。
_LABEL_ONLY_RE = re.compile(
    r"(?:解压密码|解壓密碼|解压码|解壓碼|压缩包密码|壓縮包密碼|压缩密码|壓縮密碼|"
    r"extract\s*password|archive\s*password|unzip\s*password)",
    re.IGNORECASE,
)

# 长后缀优先：分卷后缀整体剥掉，避免把 ``part1`` / ``001`` 当成密码的一部分。
_ARCHIVE_SUFFIX_RES = (
    re.compile(r"\.part\d+\.(?:rar|zip|7z|exe)$", re.IGNORECASE),
    re.compile(r"\.r\d{2}$", re.IGNORECASE),
    re.compile(r"\.z\d{2}$", re.IGNORECASE),
    re.compile(r"\.(?:7z|zip)\.\d{3}$", re.IGNORECASE),
    re.compile(r"\.(?:zip|rar|7z|tar|gz|bz2|xz|tgz|tbz2|txz|iso|exe)$", re.IGNORECASE),
    # 非压缩包扩展名只收窄名单：避免把带点的真密码误剥。
    re.compile(r"\.(?:txt|md|url|html?|jpe?g|png|gif|mp4|mkv|pdf|docx?|xlsx?|apk)$", re.IGNORECASE),
)

_LABELED_PASSWORD_TRAILING = "()[]{}<>（）【】《》「」'\"`。，；、,;:："

# 剥掉后缀 / 括号后仍然只是「扩展名或占位词」的值一律不算密码，
# 例如 ``解压码.zip`` 不能把 ``zip`` 当密码。
_LABELED_PASSWORD_TOKEN_BLOCKLIST = frozenset({
    "zip", "rar", "7z", "z01", "tar", "gz", "bz2", "xz", "exe", "iso",
    "password", "pass", "pwd", "密码", "解压码", "解压密码", "压缩包", "无", "暂无",
})

_LABELED_PASSWORD_MAX_LENGTH = 64


def _strip_archive_suffix(value: str) -> str:
    text = value
    for _ in range(2):  # 例如 ``.7z.001`` 需要剥两轮
        stripped = False
        for pattern in _ARCHIVE_SUFFIX_RES:
            new_text = pattern.sub("", text)
            if new_text != text:
                text = new_text
                stripped = True
                break
        if not stripped:
            break
    return text


def extract_labeled_password(text: Optional[str]) -> str:
    """从文件名等文本里摘出「解压码」类标签后的密码；没有则返回空串。

    只负责「有没有标签 + 紧跟的值」，不判断密码是否正确，也不写库。
    误判代价仅是多试一个候选密码，因此宁可从宽。
    """
    name = normalize_password_value(text)
    if not name:
        return ""
    for match in _LABELED_PASSWORD_RE.finditer(name):
        value = str(match.group(1) or "")
        next_label = _LABEL_ONLY_RE.search(value)
        if next_label:
            value = value[: next_label.start()]
        value = _strip_archive_suffix(value)
        value = value.strip(_LABELED_PASSWORD_TRAILING)
        value = normalize_password_value(value)
        if not value or len(value) > _LABELED_PASSWORD_MAX_LENGTH:
            continue
        if value.lower() in _LABELED_PASSWORD_TOKEN_BLOCKLIST:
            continue
        if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", value):
            continue
        return value
    return ""
