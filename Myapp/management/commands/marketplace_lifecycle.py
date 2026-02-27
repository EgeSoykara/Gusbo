import time
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from Myapp.models import SchedulerHeartbeat, SchedulerLock
from Myapp.views import refresh_marketplace_lifecycle


class Command(BaseCommand):
    help = "Runs marketplace lifecycle jobs (offer/appointment timeout and transitions)."
    worker_name = "marketplace_lifecycle"

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run continuously instead of single-shot execution.",
        )
        parser.add_argument(
            "--interval",
            type=int,
            default=30,
            help="Polling interval in seconds for --loop mode (default: 30).",
        )
        parser.add_argument(
            "--max-runs",
            type=int,
            default=0,
            help="Optional run limit for --loop mode (0 means unlimited).",
        )

    def handle(self, *args, **options):
        loop_mode = bool(options["loop"])
        interval = int(options["interval"])
        max_runs = int(options["max_runs"])

        if interval < 1:
            raise CommandError("--interval must be >= 1")
        if max_runs < 0:
            raise CommandError("--max-runs must be >= 0")

        self.lock_owner_token = uuid4().hex
        run_count = 0
        try:
            while True:
                lock_acquired = self._acquire_run_lock(interval)
                if not lock_acquired:
                    self.stdout.write(
                        self.style.WARNING(
                            "Marketplace lifecycle run skipped: another worker currently holds the lock."
                        )
                    )
                    if not loop_mode:
                        break
                    time.sleep(interval)
                    continue

                run_count += 1
                started_at = self._mark_started()
                try:
                    refresh_marketplace_lifecycle()
                except Exception as exc:
                    self._mark_error(exc)
                    raise
                self._mark_success()
                self.stdout.write(self.style.SUCCESS(f"[{started_at}] Marketplace lifecycle run #{run_count} completed."))

                if not loop_mode:
                    break
                if max_runs and run_count >= max_runs:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Marketplace lifecycle loop interrupted by user."))
        finally:
            if not loop_mode:
                self._release_run_lock()

    def _get_lock_ttl_seconds(self, interval_seconds):
        configured = int(getattr(settings, "LIFECYCLE_LOCK_TTL_SECONDS", max(interval_seconds * 3, 60)))
        return max(10, configured)

    def _acquire_run_lock(self, interval_seconds):
        now = timezone.now()
        lock_ttl_seconds = self._get_lock_ttl_seconds(interval_seconds)
        lock_until = now + timedelta(seconds=lock_ttl_seconds)

        with transaction.atomic():
            lock, _ = SchedulerLock.objects.select_for_update().get_or_create(
                worker_name=self.worker_name,
                defaults={
                    "lock_owner": self.lock_owner_token,
                    "locked_until": lock_until,
                    "last_acquired_at": now,
                },
            )

            lock_is_held_by_other = (
                lock.lock_owner
                and lock.lock_owner != self.lock_owner_token
                and lock.locked_until is not None
                and lock.locked_until > now
            )
            if lock_is_held_by_other:
                return False

            lock.lock_owner = self.lock_owner_token
            lock.locked_until = lock_until
            lock.last_acquired_at = now
            lock.save(update_fields=["lock_owner", "locked_until", "last_acquired_at", "updated_at"])
            return True

    def _release_run_lock(self):
        now = timezone.now()
        with transaction.atomic():
            lock = SchedulerLock.objects.select_for_update().filter(worker_name=self.worker_name).first()
            if not lock:
                return
            if lock.lock_owner != self.lock_owner_token:
                return

            lock.lock_owner = ""
            lock.locked_until = now - timedelta(seconds=1)
            lock.save(update_fields=["lock_owner", "locked_until", "updated_at"])

    def _mark_started(self):
        now = timezone.now()
        heartbeat, _ = SchedulerHeartbeat.objects.get_or_create(worker_name=self.worker_name)
        SchedulerHeartbeat.objects.filter(pk=heartbeat.pk).update(
            run_count=F("run_count") + 1,
            last_started_at=now,
            last_error="",
            updated_at=now,
        )
        return timezone.localtime(now).strftime("%Y-%m-%d %H:%M:%S")

    def _mark_success(self):
        now = timezone.now()
        SchedulerHeartbeat.objects.filter(worker_name=self.worker_name).update(
            last_success_at=now,
            last_error="",
            updated_at=now,
        )

    def _mark_error(self, exc):
        now = timezone.now()
        SchedulerHeartbeat.objects.filter(worker_name=self.worker_name).update(
            last_error_at=now,
            last_error=str(exc)[:240],
            updated_at=now,
        )
