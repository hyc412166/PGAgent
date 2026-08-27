"""Run coordination, configuration, delegation, event streaming, and recovery.

Import concrete responsibilities from their modules, for example
``src.runs.lifecycle`` or ``src.runs.stream``. Keeping this package initializer
side-effect free prevents the context and run domains from loading each other
during package discovery.
"""
