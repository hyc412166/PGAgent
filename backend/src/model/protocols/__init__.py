"""Wire protocol adapters. Agent orchestration is shared across protocols."""
# 文件职责：负责模型连接、请求、协议转换与流式响应中的 __init__ 子模块。
# 逻辑关系：上层通过 model/protocols/__init__.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。
