"""把 platform/ 加入 import 路径，便于直接导入 control_plane / policy_plugin。"""
import os
import sys

PLATFORM_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "platform")
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)
