from autorole.workers.scoring import ScoringWorker
from autorole.workers.tailoring import TailoringWorker

QualificationWorker = TailoringWorker

__all__ = ["ScoringWorker", "TailoringWorker", "QualificationWorker"]
