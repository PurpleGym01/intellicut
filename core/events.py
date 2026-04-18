class EventBus:
    def __init__(self):
        self._subscribers = []

    def subscribe(self, callback):
        self._subscribers.append(callback)

    def notify(self, event_data):
        for callback in self._subscribers:
            callback(event_data)

event_bus = EventBus()