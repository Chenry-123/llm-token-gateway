# -*- coding: utf-8 -*-
"""让 pytest 能 import 上一级目录（gateway/）里的模块。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
