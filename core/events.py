from utils.logger import logger_service


class EventBus:
    def __init__(self):
        self._subscribers = []
        self._logger = logger_service.get_logger()

    def subscribe(self, callback):
        self._subscribers.append(callback)

    def notify(self, event_data):
        for callback in self._subscribers:
            try:
                callback(event_data)
            except Exception as exc:
                self._logger.error(f"Event subscriber failed: {exc}")


event_bus = EventBus()