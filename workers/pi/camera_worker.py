import time
import datetime
import json
import redis
import threading
import sys
import os
import RPi.GPIO as GPIO
from picamera import PiCamera
from .worker import Worker
sys.path.append('..')

from logger.Logger import Logger, LOG_LEVEL


class CameraWorker(Worker):
	def __init__(self, config, main_thread_running, system_ready, camera_available):
		super().__init__(config, main_thread_running, system_ready)
		self.pending_reset = False

		# Events
		self.camera_available = camera_available

		# Dynamic Properties based on config
		self.path = self.config['path'].replace(" ", "-") if self.config['path'] is not None else '/etc/mudpi/img/'
		self.topic = self.config['topic'].replace(" ", "/").lower() if self.config['topic'] is not None else 'mudpi/camera/'
		if self.config['resolution'] is not None:
			self.resolutionX = int(self.config['resolution'].get('x', 1920))
			self.resolutionY = int(self.config['resolution'].get('y', 1080))
		if self.config['delay'] is not None:
			self.hours = int(self.config['delay'].get('hours', 0))
			self.minutes = int(self.config['delay'].get('minutes', 0))
			self.seconds = int(self.config['delay'].get('seconds', 0))

		self.init()
		return

	def init(self):
		try:
			self.camera = PiCamera(resolution=(self.resolutionX, self.resolutionY))
			# Below we calibrate the camera for consistent imaging
			self.camera.framerate = 30
			# Wait for the automatic gain control to settle
			time.sleep(2)
			# Now fix the values
			self.camera.shutter_speed = self.camera.exposure_speed
			self.camera.exposure_mode = 'off'
			g = self.camera.awb_gains
			self.camera.awb_mode = 'off'
			self.camera.awb_gains = g
		except:
			self.camera = PiCamera()

		#Pubsub Listeners
		self.pubsub = self.r.pubsub()
		self.pubsub.subscribe(**{self.topic: self.handleEvent})

		Logger.log(LOG_LEVEL["info"], 'Camera Worker...\t\t\t\033[1;32m Ready\033[0;0m')
		return

	def run(self): 
		t = threading.Thread(target=self.work, args=())
		t.start()
		self.listener = threading.Thread(target=self.listen, args=())
		self.listener.start()
		Logger.log(LOG_LEVEL["info"], 'Camera Worker...\t\t\t\033[1;32m Online\033[0;0m')
		return t

	def wait(self):
		# Calculate the delay
		try:
			self.next_time = (datetime.datetime.now() + datetime.timedelta(hours=self.hours, minutes=self.minutes, seconds=self.seconds)).replace(microsecond=0)
		except:
			# Default every hour
			self.next_time = (datetime.datetime.now() + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
		delay = (self.next_time - datetime.datetime.now()).seconds
		time.sleep(delay)

	def handleEvent(self, message):
		data = message['data']
		decoded_message = None
		if data is not None:
			try:
				if isinstance(data, dict):
					decoded_message = data
				elif isinstance(data.decode('utf-8'), str):
					temp = json.loads(data.decode('utf-8'))
					decoded_message = temp
					if decoded_message['event'] == 'Timelapse':
						Logger.log(LOG_LEVEL["info"], "Camera Signaled for Reset")
						self.camera_available.clear()
						self.pending_reset = True
			except:
				Logger.log(LOG_LEVEL["error"], 'Error Handling Event for Camera')

	def listen(self):
		while self.main_thread_running.is_set():
			if self.system_ready.is_set():
				if self.camera_available.is_set():
					self.pubsub.get_message()
					time.sleep(1)
				else:
					delay = (self.next_time - datetime.datetime.now()).seconds + 15
					time.sleep(delay) #wait 15 seconds after next scheduled picture
					self.camera_available.set()
			else:
				time.sleep(2)
		return

	def work(self):
		self.resetElapsedTime()
		while self.main_thread_running.is_set():
			if self.system_ready.is_set():
				if self.camera_available.is_set():
					# try:
					for i, filename in enumerate(self.camera.capture_continuous(self.path + 'mudpi-{counter:05d}.jpg')):
						if not self.camera_available.is_set():
							if self.pending_reset:
								try:
									os.remove(filename) #cleanup previous file
									self.pending_reset = False
								except:
									Logger.log(LOG_LEVEL["error"], "Error During Camera Reset Cleanup")
							break;
						message = {'event':'StateChanged', 'data':filename}
						self.r.set('last_camera_image', filename)
						self.r.publish(self.topic, json.dumps(message))
						Logger.log(LOG_LEVEL["debug"], 'Image Captured \033[1;36m%s\033[0;0m' % filename)
						self.wait()
					# except:
					# 	print("Camera Worker \t\033[1;31m Unexpected Error\033[0;0m")
					# 	time.sleep(30)
				else:
					time.sleep(1)
					self.resetElapsedTime()
			else:
				#System not ready camera should be off
				time.sleep(1)
				self.resetElapsedTime()
				
			time.sleep(0.1)

		#This is only ran after the main thread is shut down
		self.camera.close()
		self.listener.join()
		self.pubsub.close()
		Logger.log(LOG_LEVEL["info"], "Camera Worker Shutting Down...\t\t\033[1;32m Complete\033[0;0m")