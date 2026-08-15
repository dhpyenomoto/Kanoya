"""鹿のや レベニュー・インテリジェンス。

Google Places API のクチコミ件数の増分から、コンプセットの需要を推定する。
レートは推定しない（Places API は返さない）。
"""

from .config import Config, load_config
from .pipeline import Report, build_report

__all__ = ["Config", "Report", "build_report", "load_config"]
__version__ = "0.1.0"
