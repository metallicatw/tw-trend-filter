"""台股順勢交易篩選系統 —— 每天在 GitHub Actions 上跑一次的版本。

對外只有兩個名字：``run``（跑一次完整流程）與 ``VERSION``。其餘都是 pipeline
的內部細節，會隨著篩選規則調整而變。
"""

from .pipeline import VERSION, run

__all__ = ['VERSION', 'run']
