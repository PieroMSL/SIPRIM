from __future__ import annotations

import os

from celery import Celery

from backend.infrastructure.config.settings import get_settings

settings = get_settings()
celery_app = Celery("siprim", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_routes={
        "siprim.economic.process": {"queue": "economic"},
        "siprim.platform.complete": {"queue": "platform"},
        "siprim.platform.started": {"queue": "platform"},
        "siprim.platform.publish_outbox": {"queue": "platform"},
    },
    beat_schedule={
        "recover-platform-outbox": {
            "task": "siprim.platform.publish_outbox",
            "schedule": 15.0,
        }
    },
)


class CeleryTaskDispatcher:
    def dispatch_economic(self, payload: dict) -> None:
        # Vercel runs request handlers, not persistent Celery/Redis workers.
        # Reuse the same idempotent processors directly on serverless deployments.
        # The local/Docker deployment keeps the original asynchronous queue.
        if os.getenv("VERCEL"):
            from backend.application.services.pmv1_services import (
                CompletionProcessor,
                EconomicProcessor,
                EvaluationStartedProcessor,
            )

            EvaluationStartedProcessor(settings.platform_db_url).process(
                payload["evaluation_id"]
            )
            completion = EconomicProcessor(settings.economic_db_url).process(payload)
            CompletionProcessor(settings.platform_db_url).process(completion)
            return

        celery_app.send_task(
            "siprim.economic.process", args=[payload], queue="economic"
        )
