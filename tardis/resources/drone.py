from .dronestates import RequestedState
from cobald.interfaces.pool import Pool

import asyncio
import uuid


class Drone(Pool):
    def __init__(self, site_agent, batch_system_agent, observers=None, drone_id=None, state=RequestedState()):
        self._site_agent = site_agent
        self._batch_system_agent = batch_system_agent
        self._observers = observers or []
        self._state = state
        self.id = drone_id or uuid.uuid4()
        self.dns_name = '{}-{}.{}'.format('tardis', self.id, self.site_agent.site_name)
        self._allocation = 0.0
        self._demand = 0.0
        self._supply = 0.0
        self._utilisation = 0.0

    @property
    def allocation(self) -> float:
        return self._allocation

    @property
    def demand(self):
        return self._demand

    @demand.setter
    def demand(self, value):
        self._demand = value

    @property
    def supply(self):
        return self._supply

    @property
    def utilisation(self) -> float:
        return self._utilisation

    @property
    def site_agent(self):
        return self._site_agent

    async def run(self):
        while True:
            await self.state.run(self)
            await asyncio.sleep(1)

    def register_observers(self, agent):
        self._observers.append(agent)

    def remove_observers(self, agent):
        self._observers.remove(agent)

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, state):
        self._state = state
        self.notify_observers()

    def notify_observers(self):
        for observer in self._observers:
            yield from observer.notify(self)
