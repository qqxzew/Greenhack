# simulation/runner.py

import threading
from core.pipeline    import OptimizationPipeline
from simulation.agent import SimulatedAgent


DEFAULT_FLEET = [
    ("agent-finance-1",  "normal",   1.5),
    ("agent-finance-2",  "normal",   1.0),
    ("agent-hr-1",       "normal",   0.8),
    ("agent-pipeline-1", "wasteful", 1.2),
    ("agent-pipeline-2", "stuck",    0.5),
]


def build_fleet(pipeline: OptimizationPipeline,
                spec: list[tuple[str, str, float]] | None = None
                ) -> list[SimulatedAgent]:
    spec = spec or DEFAULT_FLEET
    return [SimulatedAgent(aid, beh, pipeline, rate) for aid, beh, rate in spec]


def start_threads(agents: list[SimulatedAgent], duration: float) -> list[threading.Thread]:
    threads = [
        threading.Thread(target=a.run, args=(duration,), daemon=True)
        for a in agents
    ]
    for t in threads:
        t.start()
    return threads
