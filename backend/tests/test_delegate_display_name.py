from src.tools.builtins import normalize_delegate_specs, _call_task_delegate
from src.tools.types import ToolResult


def test_names_survive_single_and_batch_normalization():
    single, code, _ = normalize_delegate_specs('实现转换', 'coding', display_name='转换实现')
    assert code is None
    assert single[0]['display_name'] == '转换实现'
    batch, code, _ = normalize_delegate_specs(tasks=[{'task': '审查测试', 'agent_id': 'review', 'display_name': '测试审查'}])
    assert code is None
    assert batch[0]['display_name'] == '测试审查'


def test_name_is_forwarded_to_runtime():
    captured = {}
    def delegate(task, **kwargs):
        captured.update(kwargs)
        return ToolResult('task', True, 'done')
    _call_task_delegate(delegate, '实现转换', 'coding', call_id='call', display_name='转换实现')
    assert captured['display_name'] == '转换实现'


def test_invalid_name_is_visible_error():
    _, code, _ = normalize_delegate_specs('实现转换', 'coding', display_name='a' * 81)
    assert code == 'invalid_delegate_name'
