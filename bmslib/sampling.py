import datetime
import time

import bmslib.bt
from bmslib.pwmath import Integrator
from bmslib.util import get_logger
from mqtt_util import publish_sample, publish_cell_voltages, publish_temperatures, publish_hass_discovery
import paho.mqtt.subscribe as subscribe
import asyncio

logger = get_logger(verbose=False)


class BmsSampler():

    def __init__(self, bms: bmslib.bt.BtBms, mqtt_client, dt_max, invert_current=False):
        self.bms = bms
        self.current_integrator = Integrator(dx_max=dt_max)
        self.power_integrator = Integrator(dx_max=dt_max)
        self.power_integrator_pos = Integrator(dx_max=dt_max, reset=True)
        self.power_integrator_neg = Integrator(dx_max=dt_max, reset=True)
        self.mqtt_client = mqtt_client
        self.invert_current = invert_current
        self.num_samples = 0

        mqtt_client.on_message = self.on_message
        mqtt_client.subscribe("battery1/output/")
        mqtt_client.loop_start()

    async def __call__(self):
        return await self.sample()

    def on_message(self, client, userdata, msg):
        if not self.bms.client.is_connected:
            return

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError as e:
            if str(e).startswith('There is no current event loop in thread'):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            else:
                raise

        loop = asyncio.get_event_loop()

        message_content = msg.payload.decode('utf-8')
        try:
            if message_content == '1':
                coroutine = self.bms.enableOutput()
            else:
                coroutine = self.bms.disableOutput()
            loop.run_until_complete(coroutine)
        except Exception:
            pass
        print(f"Message received [{msg.topic}]: {msg.payload}")

    async def sample(self):
        bms = self.bms
        mqtt_client = self.mqtt_client

        was_connected = bms.client.is_connected

        if not was_connected:
            logger.info('connecting bms %s', bms)
        t_conn = time.time()

        try:
            async with bms:
                if not was_connected:
                    logger.info('connected bms %s!', bms)
                t_fetch = time.time()
                sample = await bms.fetch()
                t_sample = time.time()

                if self.invert_current:
                    sample = sample.invert_current()

                self.current_integrator += (t_sample, sample.current)
                self.power_integrator += (t_sample, sample.power)

                if sample.power < 0:
                    self.power_integrator_neg += (t_sample, sample.power)
                else:
                    self.power_integrator_pos += (t_sample, sample.power)

                publish_sample(mqtt_client, device_topic=bms.name, sample=sample)
                logger.info('%s result@%s %s', bms.name, datetime.datetime.now().isoformat(), sample)

                voltages = await bms.fetch_voltages()
                publish_cell_voltages(mqtt_client, device_topic=bms.name, voltages=voltages)

                temperatures = sample.temperatures or await bms.fetch_temperatures()
                publish_temperatures(mqtt_client, device_topic=bms.name, temperatures=temperatures)
                logger.info('%s volt=%s temp=%s', bms.name, ','.join(map(str, voltages)), temperatures)

                # publish home assistant discovery every 60 samples
                if (self.num_samples % 60) == 0:
                    publish_hass_discovery(mqtt_client, device_topic=bms.name,
                                           num_cells=len(voltages), num_temp_sensors=len(temperatures))

                self.num_samples += 1
                t_disc = time.time()

        except Exception as ex:
            logger.error('%s error: %s', bms.name, str(ex) or str(type(ex)))
            raise

        logger.info('%s times: connect=%.2fs fetch=%.2fs', bms, t_fetch - t_conn, t_disc - t_fetch)