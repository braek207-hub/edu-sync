# -*- coding: utf-8 -*-
"""«Остановить» прогон модулей: SIGTERM → чистый выход с кодом 143.

Кнопка в Panda-BI ставит sync_requests.cancel_requested_at, диспетчер хоста
(infra/panda/sync/dispatch.sh, cancel_one) делает systemctl stop, systemd шлёт SIGTERM.
Голый Python от него умирает на месте: строка module_runs остаётся «идёт» до
fail_stale_runs (80 мин), а применение одобренного могло оборваться между запросом
в Директ и записью итога — тогда следующий такт применил бы находку второй раз.

Поэтому обработчик только поднимает флаг, а исключение Cancelled бросается в безопасных
местах:
  - сразу, если процесс внутри interruptible(): там только чтение (скан кабинетов модулем,
    пробный прогон) — находки скана раннер пишет уже после module.run;
  - иначе на ближайшем check(): между модулями, после фазы применения, между превью.
Cancelled — BaseException: модули ловят Exception на каждом кабинете («кабинет не должен
ронять остальные») и проглотили бы остановку как сбой одного логина.

На остановку у юнита TimeoutStopSec (90 с по умолчанию systemd), потом SIGKILL: применение,
которое идёт дольше, обрывается как раньше, но такое применение — десятки запросов, а не минуты.
"""

import signal
from contextlib import contextmanager
from typing import Iterator

# Код выхода остановленного прогона: 128 + SIGTERM. panda-sync@.service считает его успехом
# (SuccessExitStatus=143) — без OnFailure-алерта «упал» в Telegram.
EXIT_CODE = 143
TEXT = "остановлен вручную"


class Cancelled(BaseException):
    """Прогон остановлен (SIGTERM). Не Exception — см. докстроку модуля."""


class Switch:
    def __init__(self) -> None:
        self.requested = False
        self._depth = 0

    def request(self, *_args) -> None:
        """Обработчик SIGTERM (signum, frame) и прямой вызов в тестах."""
        self.requested = True
        if self._depth:
            raise Cancelled()

    def check(self) -> None:
        if self.requested:
            raise Cancelled()

    @contextmanager
    def interruptible(self) -> Iterator[None]:
        """Участок только чтения: остановка прерывает его сразу, не дожидаясь конца."""
        self.check()
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1


SWITCH = Switch()


def install(switch: Switch = SWITCH) -> None:
    signal.signal(signal.SIGTERM, switch.request)
