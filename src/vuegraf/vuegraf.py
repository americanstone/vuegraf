#!/usr/bin/env python3

import datetime
import json
import signal
import sys
import time
import traceback
from threading import Event

# InfluxDB v1
import influxdb

# InfluxDB v2
import influxdb_client

from pyemvue import PyEmVue
from pyemvue.enums import Scale, Unit

# flush=True helps when running in a container without a tty attached
# (alternatively, "python -u" or PYTHONUNBUFFERED will help here)
def log(level, msg):
    now = datetime.datetime.utcnow()
    print('{} | {} | {}'.format(now, level.ljust(5), msg), flush=True)

def info(msg):
    log("INFO", msg)

def error(msg):
    log("ERROR", msg)

def handleExit(signum, frame):
    global running
    error('Caught exit signal')
    running = False
    pauseEvent.set()

def getConfigValue(key, defaultValue):
    if key in config:
        return config[key]
    return defaultValue

try:
    if len(sys.argv) != 2:
        print('Usage: python {} <config-file>'.format(sys.argv[0]))
        sys.exit(1)

    configFilename = sys.argv[1]
    config = {}
    with open(configFilename) as configFile:
        config = json.load(configFile)

    influxVersion = 1
    if 'version' in config['influxDb']:
        influxVersion = config['influxDb']['version']

    bucket = ''
    write_api = None
    query_api = None
    if influxVersion == 2:
        info('Using InfluxDB version 2')
        bucket = config['influxDb']['bucket']
        org = config['influxDb']['org']
        token = config['influxDb']['token']
        url= config['influxDb']['url']
        influx2 = influxdb_client.InfluxDBClient(
           url=url,
           token=token,
           org=org
        )
        write_api = influx2.write_api(write_options=influxdb_client.client.write_api.SYNCHRONOUS)
        query_api = influx2.query_api()

        if config['influxDb']['reset']:
            info('Resetting database')
            delete_api = influx2.delete_api()
            start = "1970-01-01T00:00:00Z"
            stop = datetime.datetime.utcnow().isoformat(timespec='seconds')
            delete_api.delete(start, stop, '_measurement="energy_usage"', bucket=bucket, org=org)    
    else:
        info('Using InfluxDB version 1')
        # Only authenticate to ingress if 'user' entry was provided in config
        if 'user' in config['influxDb']:
            influx = influxdb.InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'], username=config['influxDb']['user'], password=config['influxDb']['pass'], database=config['influxDb']['database'])
        else:
            influx = influxdb.InfluxDBClient(host=config['influxDb']['host'], port=config['influxDb']['port'], database=config['influxDb']['database'])

        influx.create_database(config['influxDb']['database'])

        if config['influxDb']['reset']:
            info('Resetting database')
            influx.delete_series(measurement='energy_usage')

    running = True

    def populateDevices(account):
        deviceIdMap = {}
        account['deviceIdMap'] = deviceIdMap
        channelIdMap = {}
        account['channelIdMap'] = channelIdMap
        devices = account['vue'].get_devices()
        for device in devices:
            device = account['vue'].populate_device_properties(device)
            deviceIdMap[device.device_gid] = device
            for chan in device.channels:
                key = "{}-{}".format(device.device_gid, chan.channel_num)
                if chan.name is None and chan.channel_num == '1,2,3':
                    chan.name = device.device_name
                channelIdMap[key] = chan
                info("Discovered new channel: {} ({})".format(chan.name, chan.channel_num))

    def lookupDeviceName(account, device_gid):
        if device_gid not in account['deviceIdMap']:
            populateDevices(account)

        deviceName = "{}".format(device_gid)
        if device_gid in account['deviceIdMap']:
            deviceName = account['deviceIdMap'][device_gid].device_name
        return deviceName

    def lookupChannelName(account, chan):
        if chan.device_gid not in account['deviceIdMap']:
            populateDevices(account)

        deviceName = lookupDeviceName(account, chan.device_gid)
        name = "{}-{}".format(deviceName, chan.channel_num)
        if 'devices' in account:
            for device in account['devices']:
                if 'name' in device and device['name'] == deviceName:
                    try:
                        num = int(chan.channel_num)
                        if 'channels' in device and len(device['channels']) >= num:
                            name = device['channels'][num - 1]
                    except:
                        name = deviceName
        return name

    signal.signal(signal.SIGINT, handleExit)
    signal.signal(signal.SIGHUP, handleExit)

    pauseEvent = Event()

    intervalSecs=getConfigValue("updateIntervalSecs", 60)
    lagSecs=getConfigValue("lagSecs", 5)

    while running:
        for account in config["accounts"]:
            if 'vue' not in account:
                account['vue'] = PyEmVue()
                account['vue'].login(username=account['email'], password=account['password'])
                info('Login completed')
                populateDevices(account)

            try:
                timestamp = datetime.datetime.utcnow() - datetime.timedelta(seconds=lagSecs)
                deviceGids = list(account['deviceIdMap'].keys())
                channels = account['vue'].get_devices_usage(deviceGids, timestamp, scale=Scale.MINUTE.value, unit=Unit.KWH.value)
                if channels is not None:
                    usageDataPoints = []
                    minutesInAnHour = 60
                    wattsInAKw = 1000
                    for chan in channels:
                        chanName = lookupChannelName(account, chan)
                        kwhUsage = chan.usage
                        if kwhUsage is not None:
                            watts = float(minutesInAnHour * wattsInAKw) * kwhUsage
                            dataPoint = None
                            if influxVersion == 2:
                                dataPoint = influxdb_client.Point("energy_usage") \
                                    .tag("account_name", account['name']) \
                                    .tag("device_name", chanName) \
                                    .field("usage", watts) \
                                    .time(time=timestamp)
                            else:
                                dataPoint = {
                                    "measurement": "energy_usage",
                                    "tags": {
                                        "account_name": account['name'],
                                        "device_name": chanName,
                                    },
                                    "fields": {
                                        "usage": watts,
                                    },
                                    "time": timestamp
                                }
                            usageDataPoints.append(dataPoint)

                    info('Submitting datapoints to database; account="{}"; points={}'.format(account['name'], len(usageDataPoints)))
                    if influxVersion == 2:
                        write_api.write(bucket=bucket, record=usageDataPoints)
                    else:
                        influx.write_points(usageDataPoints)
            except:
                error('Failed to record new usage data: {}'.format(sys.exc_info())) 
                traceback.print_exc()

        pauseEvent.wait(intervalSecs)

    info('Finished')
except:
    error('Fatal error: {}'.format(sys.exc_info())) 
    traceback.print_exc()
