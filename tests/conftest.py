"""tests/test_api.py 是自带假存储、可直接运行的端到端脚本，
不由 pytest 收集（其模块级代码会启动 TestClient 场景）。"""
collect_ignore_glob = ["test_api.py"]
