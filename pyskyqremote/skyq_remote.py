"""Python module for accessing SkyQ box and EPG, and sending commands."""
import importlib
import json
import logging
import math
import socket
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from http import HTTPStatus
from operator import attrgetter

import pycountry
import requests
import websocket
import xmltodict

from .classes.channel import Channel
from .classes.channelepg import ChannelEPG
from .classes.channellist import ChannelList
from .classes.device import Device
from .classes.media import Media
from .classes.programme import Programme, RecordedProgramme
from .const import (
    APP_EPG,
    APP_STATUS_VISIBLE,
    COMMANDS,
    CONNECTTIMEOUT,
    CURRENT_TRANSPORT_STATE,
    CURRENT_URI,
    EPG_ERROR_NO_DATA,
    EPG_ERROR_PAST_END,
    KNOWN_COUNTRIES,
    PVR,
    REST_BASE_URL,
    REST_CHANNEL_LIST,
    REST_PATH_DEVICEINFO,
    REST_PATH_SYSTEMINFO,
    REST_RECORDING_DETAILS,
    SKY_PLAY_URN,
    SKY_STATE_NOMEDIA,
    SKY_STATE_OFF,
    SKY_STATE_ON,
    SKY_STATE_PAUSED,
    SKY_STATE_PLAYING,
    SKY_STATE_STANDBY,
    SKY_STATE_STOPPED,
    SKY_STATE_TRANSITIONING,
    SKYCONTROL,
    SOAP_ACTION,
    SOAP_CONTROL_BASE_URL,
    SOAP_DESCRIPTION_BASE_URL,
    SOAP_PAYLOAD,
    SOAP_RESPONSE,
    SOAP_USER_AGENT,
    TIMEOUT,
    UPNP_GET_MEDIA_INFO,
    UPNP_GET_TRANSPORT_INFO,
    WS_BASE_URL,
    WS_CURRENT_APPS,
    XSI,
)
from .const_test import TEST_CHANNEL_LIST

_LOGGER = logging.getLogger(__name__)


class SkyQRemote:
    """SkyQRemote is the instantiation of the SKYQ remote ccontrol."""

    commands = COMMANDS

    def __init__(self, host, port=49160, jsonPort=9006):
        """Stand up a new SkyQ box."""
        self.deviceSetup = False
        self._host = host
        self._remoteCountry = None
        self._overrideCountry = None
        self._epgCountryCode = None
        self._serialNumber = None
        self._test_channel = None
        # self._test = False
        self._port = port
        self._jsonport = jsonPort
        self._soapControlURL = None
        self._channel = None
        self._lastEpg = None
        self._programme = None
        self._recordedProgramme = None
        self._lastProgrammeEpg = None
        self._epgCache = OrderedDict()
        self._lastPvrId = None
        self._currentApp = APP_EPG
        self._channels = []
        self._error = False

        deviceInfo = self.getDeviceInformation()
        if not deviceInfo:
            return None

        self._setupDevice()

    def powerStatus(self) -> str:
        """Get the power status of the Sky Q box."""
        if not self._remoteCountry:
            self._setupRemote()

        if self._soapControlURL is None:
            return SKY_STATE_OFF

        output = self._retrieveInformation(REST_PATH_SYSTEMINFO)
        if output is None:
            return SKY_STATE_OFF
        if "activeStandby" in output and output["activeStandby"] is True:
            return SKY_STATE_STANDBY

        return SKY_STATE_ON

    def getCurrentState(self):
        """Get current state of the SkyQ box."""
        if not self._remoteCountry:
            self._setupRemote()

        if self._soapControlURL is None:
            return SKY_STATE_OFF

        response = self._callSkySOAPService(UPNP_GET_TRANSPORT_INFO)
        if response is not None:
            state = response[CURRENT_TRANSPORT_STATE]
            if state == SKY_STATE_NOMEDIA or state == SKY_STATE_STOPPED:
                return SKY_STATE_STANDBY
            if state == SKY_STATE_PLAYING or state == SKY_STATE_TRANSITIONING:
                return SKY_STATE_PLAYING
            if state == SKY_STATE_PAUSED:
                return SKY_STATE_PAUSED
        return SKY_STATE_OFF

    def getActiveApplication(self):
        """Get the active application on Sky Q box."""
        try:
            apps = self._callSkyWebSocket(WS_CURRENT_APPS)
            if apps is None:
                return self._currentApp

            self._currentApp = next(
                a for a in apps["apps"] if a["status"] == APP_STATUS_VISIBLE
            )["appId"]

            return self._currentApp
        except Exception:
            return self._currentApp

    def getCurrentMedia(self):
        """Get the currently playing media on the SkyQ box."""
        channel = None
        imageUrl = None
        sid = None
        pvrId = None
        live = False

        response = self._callSkySOAPService(UPNP_GET_MEDIA_INFO)
        if response is not None:
            currentURI = response[CURRENT_URI]
            if currentURI is not None:
                if XSI in currentURI:
                    # Live content
                    sid = int(currentURI[6:], 16)

                    if self._test_channel:
                        sid = self._test_channel

                    live = True
                    channelNode = self._getChannelNode(sid)
                    if channelNode:
                        channel = channelNode["channel"]
                        imageUrl = self._buildChannelImageUrl(sid, channel)
                elif PVR in currentURI:
                    # Recorded content
                    pvrId = "P" + currentURI[11:]
                    live = False
        media = Media(channel, imageUrl, sid, pvrId, live)

        return media

    def getEpgData(self, sid, epgDate, days=2):
        """Get EPG data for the specified channel/date."""
        epg = f"{str(sid)} {'{:0>2d}'.format(days)} {epgDate.strftime('%Y%m%d')}"

        if sid in self._epgCache and self._epgCache[sid]["epg"] == epg:
            return self._epgCache[sid]["channel"]
        self._lastEpg = epg

        channelNo = None
        channelName = None
        channelImageUrl = None
        programmes = set()

        channelNode = self._getChannelNode(sid)
        if channelNode:
            channelNo = channelNode["channelno"]
            channelName = channelNode["channel"]
            channelImageUrl = self._buildChannelImageUrl(sid, channelName)

            for n in range(days):
                programmesData = self._remoteCountry.getEpgData(
                    sid, channelNo, epgDate + timedelta(days=n)
                )
                if len(programmesData) > 0:
                    programmes = programmes.union(programmesData)
                else:
                    break

        self._channel = ChannelEPG(
            sid, channelNo, channelName, channelImageUrl, sorted(programmes)
        )
        self._epgCache[sid] = {
            "epg": epg,
            "channel": self._channel,
            "updatetime": datetime.utcnow(),
        }
        self._epgCache = OrderedDict(
            sorted(
                self._epgCache.items(), key=lambda x: x[1]["updatetime"], reverse=True
            )
        )
        while len(self._epgCache) > 20:
            self._epgCache.popitem(last=True)

        return self._channel

    def getProgrammeFromEpg(self, sid, epgDate, queryDate):
        """Get programme from EPG for specfied time and channel."""
        sidint = int(sid)
        programmeEpg = f"{str(sidint)} {epgDate.strftime('%Y%m%d')}"
        if (
            self._lastProgrammeEpg == programmeEpg
            and queryDate < self._programme.endtime
        ):
            return self._programme

        epgData = self.getEpgData(sidint, epgDate)

        if len(epgData.programmes) == 0:
            if not self._error:
                self._error = True
                _LOGGER.info(
                    f"I0020 - Programme data not found for host: {self._host}/{self._overrideCountry} sid: {sid} : {epgDate}"
                )
                return EPG_ERROR_NO_DATA
        else:
            self._error = False

        try:
            programme = next(
                p
                for p in epgData.programmes
                if p.starttime <= queryDate and p.endtime >= queryDate
            )

            self._programme = programme
            self._lastProgrammeEpg = programmeEpg
            return programme

        except StopIteration:
            return EPG_ERROR_PAST_END

    def getCurrentLiveTVProgramme(self, sid):
        """Get current live programme on the specified channel."""
        try:
            queryDate = datetime.utcnow()
            # seconds = queryDate.strftime("%S")
            # if not self._test:
            #     queryDate = datetime.strptime(
            #         "2020-06-06 23:59:" + seconds + ".0000", "%Y-%m-%d %H:%M:%S.%f"
            #     )
            #     self._test = True
            # else:
            #     queryDate = datetime.strptime(
            #         "2020-06-07 00:00:" + seconds + ".0000", "%Y-%m-%d %H:%M:%S.%f"
            #     )
            # print(f"{self._overrideCountry} - {queryDate}")
            programme = self.getProgrammeFromEpg(sid, queryDate, queryDate)
            if not isinstance(programme, Programme):
                return None

            return programme
        except Exception as err:
            _LOGGER.exception(f"X0030 - Error occurred: {self._host} : {sid} : {err}")
            return None

    def getRecording(self, pvrId):
        """Get the recording details."""
        season = None
        episode = None
        starttime = None
        endtime = None
        programmeuuid = None
        channel = None
        imageUrl = None
        title = None

        if self._lastPvrId == pvrId:
            return self._recordedProgramme
        self._lastPvrId = pvrId

        resp = self._http_json(REST_RECORDING_DETAILS.format(pvrId))
        if "details" not in resp:
            _LOGGER.info(f"I0030 - Recording data not found for {pvrId}")
            return None

        recording = resp["details"]

        channel = recording["cn"]
        title = recording["t"]
        if "seasonnumber" in recording and "episodenumber" in recording:
            season = recording["seasonnumber"]
            episode = recording["episodenumber"]
        if "programmeuuid" in recording:
            programmeuuid = recording["programmeuuid"]
            imageUrl = self._remoteCountry.pvr_image_url.format(str(programmeuuid))
        elif "osid" in recording:
            sid = str(recording["osid"])
            imageUrl = self._buildChannelImageUrl(sid, channel)

        starttime = datetime.utcfromtimestamp(recording["ast"])
        if "finald" in recording:
            endtime = datetime.utcfromtimestamp(recording["ast"] + recording["finald"])
        elif "schd" in recording:
            endtime = datetime.utcfromtimestamp(recording["ast"] + recording["schd"])
        else:
            endtime = starttime

        self._recordedProgramme = RecordedProgramme(
            programmeuuid, starttime, endtime, title, season, episode, imageUrl, channel
        )

        return self._recordedProgramme

    def getDeviceInformation(self):
        """Get the device information from the SkyQ box."""
        deviceInfo = self._retrieveInformation(REST_PATH_DEVICEINFO)
        if not deviceInfo:
            return None

        systemInfo = self._retrieveInformation(REST_PATH_SYSTEMINFO)
        ASVersion = deviceInfo["ASVersion"]
        IPAddress = deviceInfo["IPAddress"]
        countryCode = deviceInfo["countryCode"]
        hardwareModel = systemInfo["hardwareModel"]
        hardwareName = deviceInfo["hardwareName"]
        manufacturer = systemInfo["manufacturer"]
        modelNumber = deviceInfo["modelNumber"]
        serialNumber = deviceInfo["serialNumber"]
        versionNumber = deviceInfo["versionNumber"]

        if self._overrideCountry:
            epgCountryCode = self._overrideCountry
        else:
            epgCountryCode = countryCode.upper()
        if not epgCountryCode:
            _LOGGER.error(f"E0050 - No country identified: {self._host}")
            return None

        if epgCountryCode in KNOWN_COUNTRIES:
            epgCountryCode = KNOWN_COUNTRIES[epgCountryCode]

        self._epgCountryCode = epgCountryCode

        device = Device(
            ASVersion,
            IPAddress,
            countryCode,
            epgCountryCode,
            hardwareModel,
            hardwareName,
            manufacturer,
            modelNumber,
            serialNumber,
            versionNumber,
        )
        return device

    def getChannelList(self):
        """Get Channel list for Sky Q box."""
        channels = self._getChannels()
        if not channels:
            return None

        channelitems = set()

        for c in channels:
            channelno = c["c"]
            channelname = c["t"]
            channelsid = c["sid"]
            channelImageUrl = None  # Not available yet
            sf = c["sf"]
            channel = Channel(
                channelno, channelname, channelsid, channelImageUrl, sf=sf
            )
            channelitems.add(channel)

        channelnosorted = sorted(channelitems, key=attrgetter("channelnoint"))
        self._channellist = ChannelList(
            sorted(channelnosorted, key=attrgetter("channeltype"), reverse=True)
        )

        return self._channellist

    def getChannelInfo(self, channelNo):
        """Retrieve channel information for specified channelNo."""
        if not channelNo.isnumeric():
            return None

        try:
            channel = next(c for c in self._channels if c["c"] == channelNo)
        except StopIteration:
            return None

        channelno = channel["c"]
        channelname = channel["t"]
        channelsid = channel["sid"]
        channelImageUrl = self._buildChannelImageUrl(channelsid, channelname)
        sf = channel["sf"]
        channelInfo = Channel(
            channelno, channelname, channelsid, channelImageUrl, sf=sf
        )

        return channelInfo

    def press(self, sequence):
        """Issue the specified sequence of commands to SkyQ box."""
        if isinstance(sequence, list):
            for item in sequence:
                if item.casefold() not in self.commands:
                    _LOGGER.error(f"E0010 - Invalid command: {self._host} : {item}")
                    break
                self._sendCommand(self.commands[item.casefold()])
                time.sleep(0.5)
        else:
            if sequence not in self.commands:
                _LOGGER.error(f"E0020 - Invalid command: {self._host} : {sequence}")
            else:
                self._sendCommand(self.commands[sequence.casefold()])

    def setOverrides(
        self, overrideCountry=None, test_channel=None, jsonPort=None, port=None
    ):
        """Override various items."""
        if overrideCountry:
            self._overrideCountry = overrideCountry
        if test_channel:
            self._test_channel = test_channel
        if jsonPort:
            self._jsonport = jsonPort
        if port:
            self.port = port

    def _http_json(self, path, headers=None) -> str:
        response = requests.get(
            REST_BASE_URL.format(self._host, self._jsonport, path),
            timeout=TIMEOUT,
            headers=headers,
        )
        return json.loads(response.content)

    def _getSoapControlURL(self, descriptionIndex):
        descriptionUrl = SOAP_DESCRIPTION_BASE_URL.format(self._host, descriptionIndex)
        headers = {"User-Agent": SOAP_USER_AGENT}
        try:
            resp = requests.get(descriptionUrl, headers=headers, timeout=TIMEOUT)
            if resp.status_code == HTTPStatus.OK:
                description = xmltodict.parse(resp.text)
                deviceType = description["root"]["device"]["deviceType"]
                if not (SKYCONTROL in deviceType):
                    return {"url": None, "status": "Not Found"}
                services = description["root"]["device"]["serviceList"]["service"]
                if not isinstance(services, list):
                    services = [services]
                playService = None
                for s in services:
                    if s["serviceId"] == SKY_PLAY_URN:
                        playService = s
                if playService is None:
                    return {"url": None, "status": "Not Found"}
                return {
                    "url": SOAP_CONTROL_BASE_URL.format(
                        self._host, playService["controlURL"]
                    ),
                    "status": "OK",
                }
            return {"url": None, "status": "Not Found"}
        except (requests.exceptions.Timeout):
            _LOGGER.warning(
                f"W0010 - Control URL not accessible: {self._host} : {descriptionUrl}"
            )
            return {"url": None, "status": "Error"}
        except (requests.exceptions.ConnectionError) as err:
            _LOGGER.exception(f"X0060 - Connection error: {self._host} : {err}")
            return {"url": None, "status": "Error"}
        except Exception as err:
            _LOGGER.exception(f"X0010 - Other error occurred: {self._host} : {err}")
            return {"url": None, "status": "Error"}

    def _callSkySOAPService(self, method):
        try:
            payload = SOAP_PAYLOAD.format(method)
            headers = {
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": SOAP_ACTION.format(method),
            }
            resp = requests.post(
                self._soapControlURL,
                headers=headers,
                data=payload,
                verify=True,
                timeout=TIMEOUT,
            )
            if resp.status_code == HTTPStatus.OK:
                xml = resp.text
                return xmltodict.parse(xml)["s:Envelope"]["s:Body"][
                    SOAP_RESPONSE.format(method)
                ]
            return None
        except requests.exceptions.RequestException:
            return None

    def _callSkyWebSocket(self, method):
        try:
            ws = websocket.create_connection(WS_BASE_URL.format(self._host, method))
            response = json.loads(ws.recv())
            ws.close()
            return response
        except (TimeoutError) as err:
            _LOGGER.warning(
                f"W0020 - Websocket call failed: {self._host} : {method} : {err}"
            )
            return {"url": None, "status": "Error"}
        except Exception as err:
            _LOGGER.exception(f"X0020 - Error occurred: {self._host} : {err}")
            return None

    def _sendCommand(self, code):
        commandBytes = bytearray(
            [4, 1, 0, 0, 0, 0, int(math.floor(224 + (code / 16))), code % 16]
        )

        try:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except socket.error as err:
            _LOGGER.exception(
                f"X0040 - Failed to create socket when sending command: {self._host} : {err}"
            )
            return

        try:
            client.connect((self._host, self._port))
        except Exception as err:
            _LOGGER.exception(
                f"X0050 - Failed to connect to client when sending command: {self._host} : {err}"
            )
            return

        strlen = 12
        timeout = time.time() + CONNECTTIMEOUT

        while 1:
            data = client.recv(1024)
            data = data

            if len(data) < 24:
                client.sendall(data[0:strlen])
                strlen = 1
            else:
                client.sendall(commandBytes)
                commandBytes[1] = 0
                client.sendall(commandBytes)
                client.close()
                break

            if time.time() > timeout:
                _LOGGER.error(
                    f"E0030 - Timeout error sending command: {self._host} : {str(code)}"
                )
                break

    def _buildChannelImageUrl(self, sid, channel):
        return self._remoteCountry.buildChannelImageUrl(sid, channel)

    def _getChannelNode(self, sid):
        channelNode = self._getNodeFromChannels(sid)

        if not channelNode:
            # Load the channel list for the first time.
            # It's also possible the channels may have changed since last HA restart, so reload them
            self._channels = self._getChannels()
            channelNode = self._getNodeFromChannels(sid)
            if not channelNode:
                return None

        channel = channelNode["t"]
        channelno = channelNode["c"]
        return {"channel": channel, "channelno": channelno}

    def _getChannels(self):
        # This is here because otherwise I can never validate code for a foreign device
        if self._test_channel:
            return TEST_CHANNEL_LIST
        channels = self._http_json(REST_CHANNEL_LIST)
        if channels:
            return channels["services"]

        return []

    def _getNodeFromChannels(self, sid):
        return next((s for s in self._channels if s["sid"] == str(sid)), None)

    def _setupRemote(self):
        deviceInfo = self.getDeviceInformation()
        if not deviceInfo:
            return

        if not self.deviceSetup:
            self._setupDevice()

        """Set the remote up."""
        if not self._remoteCountry and self.deviceSetup:
            SkyQCountry = self._importCountry(self._epgCountryCode)
            self._remoteCountry = SkyQCountry()

        if len(self._channels) == 0 and self._remoteCountry:
            self._channels = self._getChannels()

    def _setupDevice(self):

        url_index = 0
        self._soapControlURL = None
        while self._soapControlURL is None and url_index < 50:
            self._soapControlURL = self._getSoapControlURL(url_index)["url"]
            url_index += 1

        self.deviceSetup = True

        return

    def _retrieveInformation(self, rest_path):
        try:
            resp = self._http_json(rest_path)
            return resp
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ):
            return None
        except Exception as err:
            _LOGGER.exception(f"X0080 - Error occurred: {self._host} : {err}")
            return None

    def _importCountry(self, epgCountryCode):
        try:
            country = pycountry.countries.get(alpha_3=epgCountryCode).alpha_2.casefold()
            SkyQCountry = importlib.import_module(
                "pyskyqremote.country.remote_" + country
            ).SkyQCountry

        except (AttributeError, ModuleNotFoundError) as err:
            _LOGGER.warning(
                f"W0030 - Invalid country, defaulting to GBR : {self._host} : {epgCountryCode} : {err}"
            )

            from pyskyqremote.country.remote_gb import SkyQCountry

        return SkyQCountry
