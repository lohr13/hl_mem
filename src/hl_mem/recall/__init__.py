"""召回包。

注意：dedup.py、conflict.py、attribute_map.py 包含写入路径的领域逻辑
（去重、冲突判定、属性归一化），而非召回逻辑。为保持现有导入链，它们暂时保留
在此包中；未来重构应将这些模块迁移到 domain/claims/ 包。
"""
