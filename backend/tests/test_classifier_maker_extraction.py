from types import SimpleNamespace

import pytest

from app.core.classifier import SmartClassifier


def _make_classifier(tmp_path, monkeypatch):
    rule = SimpleNamespace(
        enabled=True, type='maker', path_template=None,
        fallback=None, rjcode_range=None,
    )
    config = SimpleNamespace(
        classification=[rule],
        storage=SimpleNamespace(library_path=str(tmp_path)),
    )
    monkeypatch.setattr(SmartClassifier, 'config', property(lambda self: config))
    return SmartClassifier()


RENAMED = '[RJ01295710][【KU100】老师「来」房间稍微打扰一下][45voicelab][(CV 餅梨あむ 乙倉ゆい)]'
METADATA = {
    'rjcode': 'RJ01295710',
    'work_name': '【KU100】老师「来」房间稍微打扰一下',
    'maker_name': '45voicelab',
    'classification_maker_name': '45voicelab',
}


class TestExtractMaker:
    @pytest.mark.parametrize('folder, expected', [
        ('[45voicelab][RJ01295710][标题]', '45voicelab'),
        ('【真社团】作品名[RJ01295710]', '真社团'),
        ('【KU100】老师「来」房间稍微打扰一下', 'KU100'),
        # 平衡括号：第二段完整取出（语义拦截交给守卫，不在提取层判断）
        (RENAMED, '【KU100】老师「来」房间稍微打扰一下'),
        ('无括号文件夹', None),
        ('[RJ01295710]', None),
    ])
    def test_extract(self, folder, expected):
        assert SmartClassifier()._extract_maker_from_folder_name(folder) == expected

    def test_no_unbalanced_output(self):
        # 任何输入都不得产出单边括号的残缺值（旧 bug 产出「【KU100」：有【无】）
        for folder in (RENAMED, '[【KU100】老师][RJ01295710]', '【KU100】标题'):
            result = SmartClassifier()._extract_maker_from_folder_name(folder)
            if result:
                assert result.count('【') == result.count('】'), result
                assert result.count('[') == result.count(']'), result


class TestTitlePrefixGuard:
    def test_variants_of_title_prefix_detected(self):
        title = METADATA['work_name']
        for value in ('【KU100', 'KU100', '【KU100】', f'【KU100】老师', title[:10]):
            assert SmartClassifier._extracted_maker_is_title_prefix(value, METADATA), value

    def test_unrelated_maker_not_blocked(self):
        assert not SmartClassifier._extracted_maker_is_title_prefix('45voicelab', METADATA)
        assert not SmartClassifier._extracted_maker_is_title_prefix('764studio', METADATA)

    def test_empty_inputs(self):
        assert not SmartClassifier._extracted_maker_is_title_prefix('', METADATA)
        assert not SmartClassifier._extracted_maker_is_title_prefix('45voicelab', {})


class TestMakerRuleGuard:
    def test_title_prefix_extraction_falls_back_to_metadata(self, tmp_path, monkeypatch):
        classifier = _make_classifier(tmp_path, monkeypatch)
        rule = classifier.config.classification[0]
        result = classifier._apply_single_rule(rule, dict(METADATA), f'/downloads/{RENAMED}')
        # 提取值是标题前缀被守卫拦截 → 回退元数据社团名（_apply_single_rule 只返回分类目录名）
        assert result == '45voicelab'

    def test_legit_folder_maker_still_overrides(self, tmp_path, monkeypatch):
        classifier = _make_classifier(tmp_path, monkeypatch)
        rule = classifier.config.classification[0]
        metadata = dict(METADATA, work_name='作品名')
        result = classifier._apply_single_rule(rule, metadata, '/downloads/【真社团】作品名[RJ01295710]')
        assert result == '真社团'

    def test_no_metadata_maker_and_title_prefix_returns_none(self, tmp_path, monkeypatch):
        classifier = _make_classifier(tmp_path, monkeypatch)
        rule = classifier.config.classification[0]
        metadata = {k: v for k, v in METADATA.items() if 'maker' not in k}
        result = classifier._apply_single_rule(rule, metadata, f'/downloads/{RENAMED}')
        assert result is None
