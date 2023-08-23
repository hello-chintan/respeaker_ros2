#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Author: furushchev <furushchev@jsk.imi.i.u-tokyo.ac.jp>

import angles
from contextlib import contextmanager
import usb.core
import usb.util
import pyaudio
import math
import numpy as np
from tf_transformations import quaternion_from_euler
import os
import rclpy
from rclpy.node import Node
from rclpy.duration import Duration
from rclpy.time import Time
from rclpy.qos import QoSProfile, QoSDurabilityPolicy
import struct
import sys
import time
from audio_common_msgs.msg import AudioData 
from geometry_msgs.msg import PoseStamped
from std_msgs.msg import Bool, Int32, ColorRGBA
# TODO: check how to replace dynamic reconfigure
#from dynamic_reconfigure.server import Server
try:
    from pixel_ring import usb_pixel_ring_v2
except IOError as e:
    print(e)
    raise RuntimeError("Check the device is connected and recognized")

try:
    from respeaker_ros2.cfg import RespeakerConfig
except Exception as e:
    print(e)
    raise RuntimeError("Need to run respeaker_gencfg.py first")


# suppress error messages from ALSA
# https://stackoverflow.com/questions/7088672/pyaudio-working-but-spits-out-error-messages-each-time
# https://stackoverflow.com/questions/36956083/how-can-the-terminal-output-of-executables-run-by-python-functions-be-silenced-i
@contextmanager
def ignore_stderr(enable=True):
    if enable:
        devnull = None
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            stderr = os.dup(2)
            sys.stderr.flush()
            os.dup2(devnull, 2)
            try:
                yield
            finally:
                os.dup2(stderr, 2)
                os.close(stderr)
        finally:
            if devnull is not None:
                os.close(devnull)
    else:
        yield


# Partly copied from https://github.com/respeaker/usb_4_mic_array
# parameter list
# name: (id, offset, type, max, min , r/w, info)
PARAMETERS = {
    'AECFREEZEONOFF': (18, 7, 'int', 1, 0, 'rw', 'Adaptive Echo Canceler updates inhibit.', '0 = Adaptation enabled', '1 = Freeze adaptation, filter only'),
    'AECNORM': (18, 19, 'float', 16, 0.25, 'rw', 'Limit on norm of AEC filter coefficients'),
    'AECPATHCHANGE': (18, 25, 'int', 1, 0, 'ro', 'AEC Path Change Detection.', '0 = false (no path change detected)', '1 = true (path change detected)'),
    'RT60': (18, 26, 'float', 0.9, 0.25, 'ro', 'Current RT60 estimate in seconds'),
    'HPFONOFF': (18, 27, 'int', 3, 0, 'rw', 'High-pass Filter on microphone signals.', '0 = OFF', '1 = ON - 70 Hz cut-off', '2 = ON - 125 Hz cut-off', '3 = ON - 180 Hz cut-off'),
    'RT60ONOFF': (18, 28, 'int', 1, 0, 'rw', 'RT60 Estimation for AES. 0 = OFF 1 = ON'),
    'AECSILENCELEVEL': (18, 30, 'float', 1, 1e-09, 'rw', 'Threshold for signal detection in AEC [-inf .. 0] dBov (Default: -80dBov = 10log10(1x10-8))'),
    'AECSILENCEMODE': (18, 31, 'int', 1, 0, 'ro', 'AEC far-end silence detection status. ', '0 = false (signal detected) ', '1 = true (silence detected)'),
    'AGCONOFF': (19, 0, 'int', 1, 0, 'rw', 'Automatic Gain Control. ', '0 = OFF ', '1 = ON'),
    'AGCMAXGAIN': (19, 1, 'float', 1000, 1, 'rw', 'Maximum AGC gain factor. ', '[0 .. 60] dB (default 30dB = 20log10(31.6))'),
    'AGCDESIREDLEVEL': (19, 2, 'float', 0.99, 1e-08, 'rw', 'Target power level of the output signal. ', '[-inf .. 0] dBov (default: -23dBov = 10log10(0.005))'),
    'AGCGAIN': (19, 3, 'float', 1000, 1, 'rw', 'Current AGC gain factor. ', '[0 .. 60] dB (default: 0.0dB = 20log10(1.0))'),
    'AGCTIME': (19, 4, 'float', 1, 0.1, 'rw', 'Ramps-up / down time-constant in seconds.'),
    'CNIONOFF': (19, 5, 'int', 1, 0, 'rw', 'Comfort Noise Insertion.', '0 = OFF', '1 = ON'),
    'FREEZEONOFF': (19, 6, 'int', 1, 0, 'rw', 'Adaptive beamformer updates.', '0 = Adaptation enabled', '1 = Freeze adaptation, filter only'),
    'STATNOISEONOFF': (19, 8, 'int', 1, 0, 'rw', 'Stationary noise suppression.', '0 = OFF', '1 = ON'),
    'GAMMA_NS': (19, 9, 'float', 3, 0, 'rw', 'Over-subtraction factor of stationary noise. min .. max attenuation'),
    'MIN_NS': (19, 10, 'float', 1, 0, 'rw', 'Gain-floor for stationary noise suppression.', '[-inf .. 0] dB (default: -16dB = 20log10(0.15))'),
    'NONSTATNOISEONOFF': (19, 11, 'int', 1, 0, 'rw', 'Non-stationary noise suppression.', '0 = OFF', '1 = ON'),
    'GAMMA_NN': (19, 12, 'float', 3, 0, 'rw', 'Over-subtraction factor of non- stationary noise. min .. max attenuation'),
    'MIN_NN': (19, 13, 'float', 1, 0, 'rw', 'Gain-floor for non-stationary noise suppression.', '[-inf .. 0] dB (default: -10dB = 20log10(0.3))'),
    'ECHOONOFF': (19, 14, 'int', 1, 0, 'rw', 'Echo suppression.', '0 = OFF', '1 = ON'),
    'GAMMA_E': (19, 15, 'float', 3, 0, 'rw', 'Over-subtraction factor of echo (direct and early components). min .. max attenuation'),
    'GAMMA_ETAIL': (19, 16, 'float', 3, 0, 'rw', 'Over-subtraction factor of echo (tail components). min .. max attenuation'),
    'GAMMA_ENL': (19, 17, 'float', 5, 0, 'rw', 'Over-subtraction factor of non-linear echo. min .. max attenuation'),
    'NLATTENONOFF': (19, 18, 'int', 1, 0, 'rw', 'Non-Linear echo attenuation.', '0 = OFF', '1 = ON'),
    'NLAEC_MODE': (19, 20, 'int', 2, 0, 'rw', 'Non-Linear AEC training mode.', '0 = OFF', '1 = ON - phase 1', '2 = ON - phase 2'),
    'SPEECHDETECTED': (19, 22, 'int', 1, 0, 'ro', 'Speech detection status.', '0 = false (no speech detected)', '1 = true (speech detected)'),
    'FSBUPDATED': (19, 23, 'int', 1, 0, 'ro', 'FSB Update Decision.', '0 = false (FSB was not updated)', '1 = true (FSB was updated)'),
    'FSBPATHCHANGE': (19, 24, 'int', 1, 0, 'ro', 'FSB Path Change Detection.', '0 = false (no path change detected)', '1 = true (path change detected)'),
    'TRANSIENTONOFF': (19, 29, 'int', 1, 0, 'rw', 'Transient echo suppression.', '0 = OFF', '1 = ON'),
    'VOICEACTIVITY': (19, 32, 'int', 1, 0, 'ro', 'VAD voice activity status.', '0 = false (no voice activity)', '1 = true (voice activity)'),
    'STATNOISEONOFF_SR': (19, 33, 'int', 1, 0, 'rw', 'Stationary noise suppression for ASR.', '0 = OFF', '1 = ON'),
    'NONSTATNOISEONOFF_SR': (19, 34, 'int', 1, 0, 'rw', 'Non-stationary noise suppression for ASR.', '0 = OFF', '1 = ON'),
    'GAMMA_NS_SR': (19, 35, 'float', 3, 0, 'rw', 'Over-subtraction factor of stationary noise for ASR. ', '[0.0 .. 3.0] (default: 1.0)'),
    'GAMMA_NN_SR': (19, 36, 'float', 3, 0, 'rw', 'Over-subtraction factor of non-stationary noise for ASR. ', '[0.0 .. 3.0] (default: 1.1)'),
    'MIN_NS_SR': (19, 37, 'float', 1, 0, 'rw', 'Gain-floor for stationary noise suppression for ASR.', '[-inf .. 0] dB (default: -16dB = 20log10(0.15))'),
    'MIN_NN_SR': (19, 38, 'float', 1, 0, 'rw', 'Gain-floor for non-stationary noise suppression for ASR.', '[-inf .. 0] dB (default: -10dB = 20log10(0.3))'),
    'GAMMAVAD_SR': (19, 39, 'float', 1000, 0, 'rw', 'Set the threshold for voice activity detection.', '[-inf .. 60] dB (default: 3.5dB 20log10(1.5))'),
    # 'KEYWORDDETECT': (20, 0, 'int', 1, 0, 'ro', 'Keyword detected. Current value so needs polling.'),
    'DOAANGLE': (21, 0, 'int', 359, 0, 'ro', 'DOA angle. Current value. Orientation depends on build configuration.')
}


class RespeakerInterface():
    VENDOR_ID = 0x2886
    PRODUCT_ID = 0x0018
    TIMEOUT = 100000

    def __init__(self, logger=None):
        self.dev = usb.core.find(idVendor=self.VENDOR_ID,
                                 idProduct=self.PRODUCT_ID)
        if not self.dev:
            raise RuntimeError("Failed to find Respeaker device")
        logger.info("Initializing Respeaker device (takes 10 seconds)")
        try:
            self.dev.reset()
        except usb.core.USBError:
            logger.error(
                "You may have to give the right permission on respeaker device. "
                "Please run the command as followings to register udev rules.\n"
                "$ roscd respeaker_ros \n"
                "$ sudo cp -f $(rospack find respeaker_ros)/config/60-respeaker.rules /etc/udev/rules.d/60-respeaker.rules \n"
                "$ sudo systemctl restart udev \n"
                "You may find further details at https://github.com/jsk-ros-pkg/jsk_3rdparty/blob/master/respeaker_ros/README.md"
            ) # NOQA
            raise
        self.pixel_ring = usb_pixel_ring_v2.PixelRing(self.dev)
        self.set_led_think()
        time.sleep(10)  # it will take 10 seconds to re-recognize as audio device
        self.set_led_trace()
        logger.info("Respeaker device initialized (Version: %s)" % self.version)

    def __del__(self):
        try:
            self.close()
        except:
            pass
        finally:
            self.dev = None

    def write(self, name, value):
        try:
            data = PARAMETERS[name]
        except KeyError:
            return

        if data[5] == 'ro':
            raise ValueError('{} is read-only'.format(name))

        id = data[0]

        # 4 bytes offset, 4 bytes value, 4 bytes type
        if data[2] == 'int':
            payload = struct.pack(b'iii', data[1], int(value), 1)
        else:
            payload = struct.pack(b'ifi', data[1], float(value), 0)

        self.dev.ctrl_transfer(
            usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, 0, id, payload, self.TIMEOUT)

    def read(self, name):
        try:
            data = PARAMETERS[name]
        except KeyError:
            return

        id = data[0]

        cmd = 0x80 | data[1]
        if data[2] == 'int':
            cmd |= 0x40

        length = 8

        response = self.dev.ctrl_transfer(
            usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, cmd, id, length, self.TIMEOUT)

        response = struct.unpack(b'ii', bytes([x for x in response]))

        if data[2] == 'int':
            result = response[0]
        else:
            result = response[0] * (2.**response[1])

        return result

    def set_led_think(self):
        self.pixel_ring.set_brightness(10)
        self.pixel_ring.think()

    def set_led_trace(self):
        self.pixel_ring.set_brightness(20)
        self.pixel_ring.trace()

    def set_led_color(self, r, g, b, a):
        self.pixel_ring.set_brightness(int(20 * a))
        self.pixel_ring.set_color(r=int(r*255), g=int(g*255), b=int(b*255))

    def set_vad_threshold(self, db):
        self.write('GAMMAVAD_SR', db)

    def is_voice(self):
        return self.read('VOICEACTIVITY')

    @property
    def direction(self):
        return self.read('DOAANGLE')

    @property
    def version(self):
        return self.dev.ctrl_transfer(
            usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, 0x80, 0, 1, self.TIMEOUT)[0]

    def close(self):
        """
        close the interface
        """
        usb.util.dispose_resources(self.dev)


class RespeakerAudio():
    def __init__(self, on_audio, channels=None, suppress_error=True, logger=None):
        self.on_audio = on_audio
        with ignore_stderr(enable=suppress_error):
            self.pyaudio = pyaudio.PyAudio()
        self.available_channels = None
        self.channels = channels
        self.device_index = None
        self.rate = self.get_parameter("sample_rate", 16000)
        self.bitwidth = self.get_parameter("sample_width", 2)
        self.bitdepth = 16

        # find device
        count = self.pyaudio.get_device_count()
        logger.debug("%d audio devices found" % count)
        for i in range(count):
            info = self.pyaudio.get_device_info_by_index(i)
            name = info["name"]
            chan = info["maxInputChannels"]
            logger.debug(" - %d: %s" % (i, name))
            if name.lower().find("respeaker") >= 0:
                self.available_channels = chan
                self.device_index = i
                logger.info("Found %d: %s (channels: %d)" % (i, name, chan))
                break
        if self.device_index is None:
            logger.warn("Failed to find respeaker device by name. Using default input")
            info = self.pyaudio.get_default_input_device_info()
            self.available_channels = info["maxInputChannels"]
            self.device_index = info["index"]

        if self.available_channels != 6:
            logger.warn("%d channel is found for respeaker" % self.available_channels)
            logger.warn("You may have to update firmware.")
        if self.channels is None:
            self.channels = range(self.available_channels)
        else:
            self.channels = filter(lambda c: 0 <= c < self.available_channels, self.channels)
        if not self.channels:
            raise RuntimeError('Invalid channels %s. (Available channels are %s)' % (
                self.channels, self.available_channels))
        logger.info('Using channels %s' % self.channels)

        self.stream = self.pyaudio.open(
            input=True, start=False,
            format=pyaudio.paInt16,
            channels=self.available_channels,
            rate=self.rate,
            frames_per_buffer=1024,
            stream_callback=self.stream_callback,
            input_device_index=self.device_index,
        )

    def __del__(self):
        self.stop()
        try:
            self.stream.close()
        except:
            pass
        finally:
            self.stream = None
        try:
            self.pyaudio.terminate()
        except:
            pass

    def stream_callback(self, in_data, frame_count, time_info, status):
        # split channel
        data = np.frombuffer(in_data, dtype=np.int16)
        chunk_per_channel = np.math.ceil(len(data) / self.available_channels)
        data = np.reshape(data, (chunk_per_channel, self.available_channels))
        for chan in self.channels:
            chan_data = bytearray(data[:, chan].tobytes())

            # invoke callback
            self.on_audio(chan_data, chan)

        return None, pyaudio.paContinue

    def start(self):
        if self.stream.is_stopped():
            self.stream.start_stream()

    def stop(self):
        if self.stream.is_active():
            self.stream.stop_stream()


class RespeakerNode(Node):
    def __init__(self):
        super().__init__("respeaker_node")
        
        self.update_rate = self.get_parameter("update_rate", 10.0)
        self.sensor_frame_id = self.get_parameter("sensor_frame_id", "respeaker_base")
        self.doa_xy_offset = self.get_parameter("doa_xy_offset", 0.0)
        self.doa_yaw_offset = self.get_parameter("doa_yaw_offset", 90.0)
        self.speech_prefetch = self.get_parameter("speech_prefetch", 0.5)
        self.speech_continuation = self.get_parameter("speech_continuation", 0.5)
        self.speech_max_duration = self.get_parameter("speech_max_duration", 7.0)
        self.speech_min_duration = self.get_parameter("speech_min_duration", 0.1)
        self.main_channel = self.get_parameter('main_channel', 0)
        suppress_pyaudio_error = self.get_parameter("suppress_pyaudio_error", True)
        #
        self.logger = self.get_logger()
        self.respeaker = RespeakerInterface(logger=self.logger)
        self.respeaker_audio = RespeakerAudio(self.on_audio, suppress_error=suppress_pyaudio_error, logger=self.logger)
        self.speech_audio_buffer = bytearray()
        self.is_speeching = False
        self.speech_stopped = Time()
        self.prev_is_voice = None
        self.prev_doa = None
        # advertise
        latching_qos = QoSProfile(depth=1,
            durability=QoSDurabilityPolicy.RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL)
        self.pub_vad = self.create_publisher(Bool, "is_speeching", qos_profile=latching_qos)
        self.pub_doa_raw = self.create_publisher(Int32, "sound_direction", qos_profile=latching_qos)
        self.pub_doa = self.create_publisher(PoseStamped, "sound_localization", qos_profile=latching_qos)
        
        self.pub_audio = self.create_publisher(AudioData, "audio", 10)
        self.pub_speech_audio = self.create_publisher(AudioData, "speech_audio", 10)
        self.pub_audios = {c:self.create_publisher(AudioData, 'audio/channel%d' % c, 10) for c in self.respeaker_audio.channels}
        # init config
        self.config = None
        # TODO: check how to replace dynamic reconfigure
        #self.dyn_srv = Server(RespeakerConfig, self.on_config)
        # start
        self.speech_prefetch_bytes = int(
            self.speech_prefetch * self.respeaker_audio.rate * self.respeaker_audio.bitdepth / 8.0)
        self.speech_prefetch_buffer = bytearray()
        self.respeaker_audio.start()
        self.info_timer = self.create_timer(1.0/self.update_rate,
                                      self.on_timer)
        self.timer_led = None
        self.sub_led = self.create_subscription(ColorRGBA, "status_led", self.on_status_led)

    def on_shutdown(self):
        try:
            self.respeaker.close()
        except:
            pass
        finally:
            self.respeaker = None
        try:
            self.respeaker_audio.stop()
        except:
            pass
        finally:
            self.respeaker_audio = None

    def on_config(self, config, level):
        if self.config is None:
            # first get value from device and set them as ros parameters
            for name in config.keys():
                config[name] = self.respeaker.read(name)
        else:
            # if there is different values, write them to device
            for name, value in config.items():
                prev_val = self.config[name]
                if prev_val != value:
                    self.respeaker.write(name, value)
        self.config = config
        return config

    def on_status_led(self, msg):
        self.respeaker.set_led_color(r=msg.r, g=msg.g, b=msg.b, a=msg.a)
        if self.timer_led and self.timer_led.is_alive():
            self.timer_led.shutdown()
        self.respeaker.set_led_trace()
        # TODO: check if setting oneshot for timer is equivalent to calling the method once
        # self.timer_led = rospy.Timer(rospy.Duration(3.0),
        #                                lambda e: self.respeaker.set_led_trace(),
        #                                oneshot=True)

    def on_audio(self, data, channel):
        self.pub_audios[channel].publish(AudioData(data=data))
        if channel == self.main_channel:
            self.pub_audio.publish(AudioData(data=data))
            if self.is_speeching:
                if len(self.speech_audio_buffer) == 0:
                    self.speech_audio_buffer = self.speech_prefetch_buffer
                for x in data:
                    self.speech_audio_buffer += bytearray([x])
            else:
                for x in data:
                    self.speech_prefetch_buffer += bytearray([x])
                self.speech_prefetch_buffer = self.speech_prefetch_buffer[-self.speech_prefetch_bytes:]

    def on_timer(self, event):
        stamp = self.get_clock().now()
        is_voice = self.respeaker.is_voice()
        doa_rad = math.radians(self.respeaker.direction - 180.0)
        doa_rad = angles.shortest_angular_distance(
            doa_rad, math.radians(self.doa_yaw_offset))
        doa = math.degrees(doa_rad)

        # vad
        if is_voice != self.prev_is_voice:
            self.pub_vad.publish(Bool(data=is_voice))
            self.prev_is_voice = is_voice

        # doa
        if doa != self.prev_doa:
            self.pub_doa_raw.publish(data=int(doa))
            self.prev_doa = doa

            msg = PoseStamped()
            msg.header.frame_id = self.sensor_frame_id
            msg.header.stamp = stamp
            ori = quaternion_from_euler(math.radians(doa), 0, 0)
            msg.pose.position.x = self.doa_xy_offset * np.cos(doa_rad)
            msg.pose.position.y = self.doa_xy_offset * np.sin(doa_rad)
            msg.pose.orientation.w = ori[0]
            msg.pose.orientation.x = ori[1]
            msg.pose.orientation.y = ori[2]
            msg.pose.orientation.z = ori[3]
            self.pub_doa.publish(msg)

        # speech audio
        if is_voice:
            self.speech_stopped = stamp
        if ((stamp - self.speech_stopped) < Duration(seconds=self.speech_continuation)):
            self.is_speeching = True
        elif self.is_speeching:
            buf = self.speech_audio_buffer
            self.speech_audio_buffer = bytearray()
            self.is_speeching = False
            duration = len(buf) * self.respeaker_audio.bitwidth * 8.0 
            duration = duration / self.respeaker_audio.rate / self.respeaker_audio.bitdepth
            self.logger.info("Speech detected for %.3f seconds" % duration)
            if self.speech_min_duration <= duration < self.speech_max_duration:
                self.pub_speech_audio.publish(AudioData(data=list(buf)))


def main():
    rclpy.init()
    respeaker_node = RespeakerNode()
    try:
        rclpy.spin(respeaker_node)
    except KeyboardInterrupt:
        pass
    finally:
        respeaker_node.on_shutdown()  # do any custom cleanup
        respeaker_node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
