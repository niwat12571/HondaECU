import sys
import os
import time

from threading import Thread

import usb1
import pylibftdi

import wx
from wx.lib.pubsub import pub
from wx.lib.mixins.listctrl import ListCtrlAutoWidthMixin

import EnhancedStatusBar as ESB

from ecu import *

checksums = [
	"0xDFEF",
	"0x18FFE",
	"0x19FFE",
	"0x1FFFA",
	"0x3FFF8",
	"0x7FFF8",
	"0xFFFF8"
]

class USBMonitor(Thread):

	def __init__(self, parent):
		self.parent = parent
		self.usbcontext = usb1.USBContext()
		self.ftdi_devices = {}
		Thread.__init__(self)

	def run(self):
		while self.parent.run:
			time.sleep(.5)
			new_devices = {}
			for device in self.usbcontext.getDeviceList(skip_on_error=True):
				try:
					if device.getVendorID() == pylibftdi.driver.FTDI_VENDOR_ID and device.getProductID() in pylibftdi.driver.USB_PID_LIST:
						serial = None
						try:
							serial = device.getSerialNumber()
						except usb1.USBErrorNotSupported:
							pass
						new_devices[device] = serial
						if not device in self.ftdi_devices.keys():
							wx.CallAfter(pub.sendMessage, "USBMonitor", action="add", device=str(device), serial=serial)
				except usb1.USBErrorPipe:
					pass
				except usb1.USBErrorNoDevice:
					pass
				except usb1.USBErrorIO:
					pass
				except usb1.USBErrorBusy:
					pass
			for device in self.ftdi_devices.keys():
				if not device in new_devices.keys():
					wx.CallAfter(pub.sendMessage, "USBMonitor", action="remove", device=str(device), serial=self.ftdi_devices[device])
			self.ftdi_devices = new_devices

class KlineWorker(Thread):

	def __init__(self, parent):
		self.parent = parent
		self.__clear_data()
		pub.subscribe(self.DeviceHandler, "HondaECU.device")
		pub.subscribe(self.ErrorPanelHandler, "ErrorPanel")
		Thread.__init__(self)

	def __cleanup(self):
		if self.ecu:
			self.ecu.dev.close()
			del self.ecu
		self.__clear_data()

	def __clear_data(self):
		self.ecu = None
		self.ready = False
		self.state = 0
		self.ecmid = None
		self.flashcount = -1
		self.dtccount = -1
		self.update_errors = True
		self.errorcodes = {}
		self.update_tables = True
		self.tables = None
		self.clear_codes = False

	def ErrorPanelHandler(self, action):
		if action == "cleardtc":
			self.clear_codes = True

	def DeviceHandler(self, action, device, serial):
		if action == "deactivate":
			if self.ecu:
				wx.LogVerbose("Deactivating device (%s | %s)" % (device, serial))
				self.__cleanup()
		elif action == "activate":
			wx.LogVerbose("Activating device (%s | %s)" % (device, serial))
			self.__clear_data()
			self.ecu = HondaECU(device_id=serial, dprint=wx.LogDebug)
			self.ecu.setup()
			self.ready = True

	def run(self):
		while self.parent.run:
			if not self.ready:
				time.sleep(.001)
			else:
				try:
					if self.state in [0,12]:
						self.state, status = self.ecu.detect_ecu_state()
						wx.CallAfter(pub.sendMessage, "KlineWorker", info="state", value=(self.state,status))
						wx.LogVerbose("ECU state: %s" % (status))
					elif self.state == 1:
						if self.ecu.ping():
							if not self.ecmid:
								info = self.ecu.send_command([0x72], [0x71, 0x00])
								if info:
									self.ecmid = info[2][2:7]
									ecmid = " ".join(["%02x" % i for i in self.ecmid])
									wx.CallAfter(pub.sendMessage, "KlineWorker", info="ecmid", value=ecmid)
									wx.LogVerbose("ECM id: %s" % (ecmid))
							if self.flashcount < 0:
								info = self.ecu.send_command([0x7d], [0x01, 0x01, 0x03])
								if info:
									self.flashcount = int(info[2][4])
									wx.CallAfter(pub.sendMessage, "KlineWorker", info="flashcount", value=self.flashcount)
									wx.Yield()
							while self.clear_codes:
								info = self.ecu.send_command([0x72],[0x60, 0x03])
								if info:
									if info[2][1] == 0x00:
										self.dtccount = -1
										self.errorcodes = {}
										self.clear_codes = False
								else:
									self.dtccount = -1
									self.errorcodes = {}
									self.clear_codes = False
								wx.Yield()
							if self.update_errors:
								errorcodes = {}
								for type in [0x74,0x73]:
									errorcodes[hex(type)] = []
									for i in range(1,0x0c):
										info = self.ecu.send_command([0x72],[type, i])[2]
										wx.Yield()
										for j in [3,5,7]:
											if info[j] != 0:
												errorcodes[hex(type)].append("%02d-%02d" % (info[j],info[j+1]))
										if info[2] == 0:
											break
								dtccount = sum([len(c) for c in errorcodes.values()])
								if self.dtccount != dtccount:
									self.dtccount = dtccount
									wx.CallAfter(pub.sendMessage, "KlineWorker", info="dtccount", value=self.dtccount)
								if self.errorcodes != errorcodes:
									self.errorcodes = errorcodes
									wx.CallAfter(pub.sendMessage, "KlineWorker", info="dtc", value=self.errorcodes)
								wx.Yield()
							if not self.tables:
								tables = self.ecu.probe_tables()
								if len(tables) > 0:
									self.tables = tables
									tables = " ".join([hex(x) for x in self.tables.keys()])
									wx.LogVerbose("HDS tables: %s" % tables)
									for t, d in self.tables.items():
										wx.CallAfter(pub.sendMessage, "KlineWorker", info="hds", value=(t,d[0],d[1]))
										wx.Yield()
							else:
								if self.update_tables:
									for t in self.tables:
										info = self.ecu.send_command([0x72], [0x71, t])
										if info:
											if info[3] > 2:
												self.tables[t] = [info[3],info[2]]
												wx.CallAfter(pub.sendMessage, "KlineWorker", info="hds", value=(t,info[3],info[2]))
												wx.Yield()
						else:
							self.state = 0
				except pylibftdi._base.FtdiError:
					pass
				except AttributeError:
					pass
				except OSError:
					pass

class ErrorListCtrl(wx.ListCtrl, ListCtrlAutoWidthMixin):
	def __init__(self, parent, ID, pos=wx.DefaultPosition,
				 size=wx.DefaultSize, style=0):
		wx.ListCtrl.__init__(self, parent, ID, pos, size, style)
		ListCtrlAutoWidthMixin.__init__(self)
		self.setResizeColumn(2)

class ErrorPanel(wx.Panel):

	def __init__(self, parent):
		wx.Panel.__init__(self, parent)

		self.errorlist = ErrorListCtrl(self, wx.ID_ANY, style=wx.LC_REPORT|wx.LC_HRULES)
		self.errorlist.InsertColumn(1,"DTC",format=wx.LIST_FORMAT_CENTER,width=50)
		self.errorlist.InsertColumn(2,"Description",format=wx.LIST_FORMAT_CENTER,width=-1)
		self.errorlist.InsertColumn(3,"Occurance",format=wx.LIST_FORMAT_CENTER,width=80)

		self.resetbutton = wx.Button(self, label="Clear Codes")
		self.resetbutton.Disable()

		self.errorsizer = wx.BoxSizer(wx.VERTICAL)
		self.errorsizer.Add(self.errorlist, 1, flag=wx.EXPAND|wx.ALL, border=10)
		self.errorsizer.Add(self.resetbutton, 0, flag=wx.ALIGN_RIGHT|wx.BOTTOM|wx.RIGHT, border=10)
		self.SetSizer(self.errorsizer)

		self.Bind(wx.EVT_BUTTON, self.OnClearCodes)

	def OnClearCodes(self, event):
		self.resetbutton.Disable()
		self.errorlist.DeleteAllItems()
		wx.CallAfter(pub.sendMessage, "ErrorPanel", action="cleardtc")

class DataPanel(wx.Panel):

	def __init__(self, parent):
		wx.Panel.__init__(self, parent)

		enginespeedl = wx.StaticText(self, label="Engine speed")
		vehiclespeedl = wx.StaticText(self, label="Vehicle speed")
		ectsensorl = wx.StaticText(self, label="ECT sensor")
		iatsensorl = wx.StaticText(self, label="IAT sensor")
		mapsensorl = wx.StaticText(self, label="MAP sensor")
		tpsensorl = wx.StaticText(self, label="TP sensor")
		batteryvoltagel = wx.StaticText(self, label="Battery")
		injectorl = wx.StaticText(self, label="Injector")
		advancel = wx.StaticText(self, label="Advance")
		iacvpl = wx.StaticText(self, label="IACV pulse count")
		iacvcl = wx.StaticText(self, label="IACV command")
		eotsensorl = wx.StaticText(self, label="EOT sensor")
		tcpsensorl = wx.StaticText(self, label="TCP sensor")
		apsensorl = wx.StaticText(self, label="AP sensor")
		racvalvel = wx.StaticText(self, label="RAC valve direction")
		o2volt1l = wx.StaticText(self, label="O2 sensor voltage #1")
		o2heat1l = wx.StaticText(self, label="O2 sensor heater #1")
		sttrim1l = wx.StaticText(self, label="ST fuel trim #1")

		self.enginespeedl = wx.StaticText(self, label="---")
		self.vehiclespeedl = wx.StaticText(self, label="---")
		self.ectsensorl = wx.StaticText(self, label="---")
		self.iatsensorl = wx.StaticText(self, label="---")
		self.mapsensorl = wx.StaticText(self, label="---")
		self.tpsensorl = wx.StaticText(self, label="---")
		self.batteryvoltagel = wx.StaticText(self, label="---")
		self.injectorl = wx.StaticText(self, label="---")
		self.advancel = wx.StaticText(self, label="---")
		self.iacvpl = wx.StaticText(self, label="---")
		self.iacvcl = wx.StaticText(self, label="---")
		self.eotsensorl = wx.StaticText(self, label="---")
		self.tcpsensorl = wx.StaticText(self, label="---")
		self.apsensorl = wx.StaticText(self, label="---")
		self.racvalvel = wx.StaticText(self, label="---")
		self.o2volt1l = wx.StaticText(self, label="---")
		self.o2heat1l = wx.StaticText(self, label="---")
		self.sttrim1l = wx.StaticText(self, label="---")

		enginespeedlu = wx.StaticText(self, label="rpm")
		vehiclespeedlu = wx.StaticText(self, label="Km/h")
		ectsensorlu = wx.StaticText(self, label="°C")
		iatsensorlu = wx.StaticText(self, label="°C")
		mapsensorlu = wx.StaticText(self, label="kPa")
		tpsensorlu = wx.StaticText(self, label="°")
		batteryvoltagelu = wx.StaticText(self, label="V")
		injectorlu = wx.StaticText(self, label="ms")
		advancelu = wx.StaticText(self, label="°")
		iacvplu = wx.StaticText(self, label="Steps")
		iacvclu = wx.StaticText(self, label="g/sec")
		eotsensorlu = wx.StaticText(self, label="°C")
		tcpsensorlu = wx.StaticText(self, label="kPa")
		apsensorlu = wx.StaticText(self, label="kPa")
		racvalvelu = wx.StaticText(self, label="l/min")
		o2volt1lu = wx.StaticText(self, label="V")

		o2volt2l = wx.StaticText(self, label="O2 sensor voltage #2")
		o2heat2l = wx.StaticText(self, label="O2 sensor heater #2")
		sttrim2l = wx.StaticText(self, label="ST fuel trim #2")
		basvl = wx.StaticText(self, label="Bank angle sensor input")
		egcvil = wx.StaticText(self, label="EGCV position input")
		egcvtl = wx.StaticText(self, label="EGCV position target")
		egcvll = wx.StaticText(self, label="EGCV load")
		lscl = wx.StaticText(self, label="Linear solenoid current")
		lstl = wx.StaticText(self, label="Linear solenoid target")
		lsvl = wx.StaticText(self, label="Linear solenoid load")
		oscl = wx.StaticText(self, label="Overflow solenoid")
		estl = wx.StaticText(self, label="Exhaust surface temp")
		icsl = wx.StaticText(self, label="Ignition cut-off switch")
		ersl = wx.StaticText(self, label="Engine run switch")
		scsl = wx.StaticText(self, label="SCS")
		fpcl = wx.StaticText(self, label="Fuel pump control")
		intakeairl = wx.StaticText(self, label="Intake AIR control valve")
		pairvl = wx.StaticText(self, label="PAIR solenoid valve")

		self.o2volt2l = wx.StaticText(self, label="---")
		self.o2heat2l = wx.StaticText(self, label="---")
		self.sttrim2l = wx.StaticText(self, label="---")
		self.basvl = wx.StaticText(self, label="---")
		self.egcvil = wx.StaticText(self, label="---")
		self.egcvtl = wx.StaticText(self, label="---")
		self.egcvll = wx.StaticText(self, label="---")
		self.lscl = wx.StaticText(self, label="---")
		self.lstl = wx.StaticText(self, label="---")
		self.lsvl = wx.StaticText(self, label="---")
		self.oscl = wx.StaticText(self, label="---")
		self.estl = wx.StaticText(self, label="---")
		self.icsl = wx.StaticText(self, label="---")
		self.ersl = wx.StaticText(self, label="---")
		self.scsl = wx.StaticText(self, label="---")
		self.fpcl = wx.StaticText(self, label="---")
		self.intakeairl = wx.StaticText(self, label="---")
		self.pairvl = wx.StaticText(self, label="---")

		o2volt2lu = wx.StaticText(self, label="V")
		basvlu = wx.StaticText(self, label="V")
		egcvilu = wx.StaticText(self, label="V")
		egcvtlu = wx.StaticText(self, label="V")
		egcvllu = wx.StaticText(self, label="%")
		lsclu = wx.StaticText(self, label="A")
		lstlu = wx.StaticText(self, label="A")
		lsvlu = wx.StaticText(self, label="%")
		osclu = wx.StaticText(self, label="%")
		estlu = wx.StaticText(self, label="°C")

		fc1l = wx.StaticText(self, label="Fan control")
		basl = wx.StaticText(self, label="Bank angle sensor")
		esl = wx.StaticText(self, label="Emergency switch")
		mstsl = wx.StaticText(self, label="MST switch")
		lsl = wx.StaticText(self, label="Limit switch")
		otssl = wx.StaticText(self, label="OTS switch")
		lysl = wx.StaticText(self, label="LY switch")
		otscl = wx.StaticText(self, label="OTS control")
		evapl = wx.StaticText(self, label="EVAP pc solenoid")
		vtecl = wx.StaticText(self, label="VTEC valve pressure switch")
		pcvl = wx.StaticText(self, label="PCV solenoid")
		startersl = wx.StaticText(self, label="Starter switch signal")
		startercl = wx.StaticText(self, label="Starter switch command")
		fc2l = wx.StaticText(self, label="Fan control 2nd level")
		gearsl = wx.StaticText(self, label="Gear position switch")
		startervl = wx.StaticText(self, label="Starter solenoid valve")
		mainrl = wx.StaticText(self, label="Main relay control")
		filampl = wx.StaticText(self, label="FI control lamp")

		self.fc1l = wx.StaticText(self, label="---")
		self.basl = wx.StaticText(self, label="---")
		self.esl = wx.StaticText(self, label="---")
		self.mstsl = wx.StaticText(self, label="---")
		self.lsl = wx.StaticText(self, label="---")
		self.otssl = wx.StaticText(self, label="---")
		self.lysl = wx.StaticText(self, label="---")
		self.otscl = wx.StaticText(self, label="---")
		self.evapl = wx.StaticText(self, label="---")
		self.vtecl = wx.StaticText(self, label="---")
		self.pcvl = wx.StaticText(self, label="---")
		self.startersl = wx.StaticText(self, label="---")
		self.startercl = wx.StaticText(self, label="---")
		self.fc2l = wx.StaticText(self, label="---")
		self.gearsl = wx.StaticText(self, label="---")
		self.startervl = wx.StaticText(self, label="---")
		self.mainrl = wx.StaticText(self, label="---")
		self.filampl = wx.StaticText(self, label="---")

		self.datapsizer = wx.GridBagSizer(1,5)

		self.datapsizer.Add(enginespeedl, pos=(0,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(vehiclespeedl, pos=(1,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(ectsensorl, pos=(2,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iatsensorl, pos=(3,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mapsensorl, pos=(4,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tpsensorl, pos=(5,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(batteryvoltagel, pos=(6,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(injectorl, pos=(7,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(advancel, pos=(8,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvpl, pos=(9,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvcl, pos=(10,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(eotsensorl, pos=(11,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tcpsensorl, pos=(12,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(apsensorl, pos=(13,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(racvalvel, pos=(14,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(o2volt1l, pos=(15,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(o2heat1l, pos=(16,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(sttrim1l, pos=(17,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.BOTTOM, border=10)

		self.datapsizer.Add(self.enginespeedl, pos=(0,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.TOP, border=10)
		self.datapsizer.Add(self.vehiclespeedl, pos=(1,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.ectsensorl, pos=(2,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.iatsensorl, pos=(3,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.mapsensorl, pos=(4,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.tpsensorl, pos=(5,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.batteryvoltagel, pos=(6,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.injectorl, pos=(7,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.advancel, pos=(8,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.iacvpl, pos=(9,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.iacvcl, pos=(10,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.eotsensorl, pos=(11,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.tcpsensorl, pos=(12,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.apsensorl, pos=(13,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.racvalvel, pos=(14,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.o2volt1l, pos=(15,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.o2heat1l, pos=(16,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.sttrim1l, pos=(17,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.BOTTOM, border=10)

		self.datapsizer.Add(enginespeedlu, pos=(0,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(vehiclespeedlu, pos=(1,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(ectsensorlu, pos=(2,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iatsensorlu, pos=(3,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mapsensorlu, pos=(4,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tpsensorlu, pos=(5,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(batteryvoltagelu, pos=(6,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(injectorlu, pos=(7,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(advancelu, pos=(8,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvplu, pos=(9,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvclu, pos=(10,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(eotsensorlu, pos=(11,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tcpsensorlu, pos=(12,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(apsensorlu, pos=(13,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(racvalvelu, pos=(14,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(o2volt1lu, pos=(15,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)

		self.datapsizer.Add(o2volt2l, pos=(0,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(o2heat2l, pos=(1,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(sttrim2l, pos=(2,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(basvl, pos=(3,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvil, pos=(4,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvtl, pos=(5,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvll, pos=(6,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lscl, pos=(7,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lstl, pos=(8,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsvl, pos=(9,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(oscl, pos=(10,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(estl, pos=(11,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(icsl, pos=(12,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(ersl, pos=(13,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(scsl, pos=(14,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(fpcl, pos=(15,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(intakeairl, pos=(16,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(pairvl, pos=(17,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.BOTTOM, border=10)

		self.datapsizer.Add(self.o2volt2l, pos=(0,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.TOP, border=10)
		self.datapsizer.Add(self.o2heat2l, pos=(1,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.sttrim2l, pos=(2,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.basvl, pos=(3,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.egcvil, pos=(4,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.egcvtl, pos=(5,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.egcvll, pos=(6,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.lscl, pos=(7,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.lstl, pos=(8,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.lsvl, pos=(9,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.oscl, pos=(10,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.estl, pos=(11,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.icsl, pos=(12,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.ersl, pos=(13,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.scsl, pos=(14,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.fpcl, pos=(15,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.intakeairl, pos=(16,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.pairvl, pos=(17,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.BOTTOM, border=10)

		self.datapsizer.Add(o2volt2lu, pos=(0,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(basvlu, pos=(3,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvilu, pos=(4,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvtlu, pos=(5,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvllu, pos=(6,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsclu, pos=(7,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lstlu, pos=(8,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsvlu, pos=(9,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(osclu, pos=(10,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(estlu, pos=(11,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)

		self.datapsizer.Add(fc1l, pos=(0,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(basl, pos=(1,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(esl, pos=(2,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mstsl, pos=(3,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsl, pos=(4,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(otssl, pos=(5,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lysl, pos=(6,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(otscl, pos=(7,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(evapl, pos=(8,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(vtecl, pos=(9,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(pcvl, pos=(10,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(startersl, pos=(11,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(startercl, pos=(12,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(fc2l, pos=(13,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(gearsl, pos=(14,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(startervl, pos=(15,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mainrl, pos=(16,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(filampl, pos=(17,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.BOTTOM, border=10)

		self.datapsizer.Add(self.fc1l, pos=(0,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT|wx.TOP, border=10)
		self.datapsizer.Add(self.basl, pos=(1,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.esl, pos=(2,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.mstsl, pos=(3,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.lsl, pos=(4,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.otssl, pos=(5,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.lysl, pos=(6,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.otscl, pos=(7,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.evapl, pos=(8,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.vtecl, pos=(9,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.pcvl, pos=(10,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.startersl, pos=(11,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.startercl, pos=(12,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.fc2l, pos=(13,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.gearsl, pos=(14,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.startervl, pos=(15,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.mainrl, pos=(16,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.filampl, pos=(17,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT|wx.BOTTOM, border=10)

		self.datapsizer.AddGrowableCol(3,1)
		self.datapsizer.AddGrowableCol(7,1)
		for r in range(18):
			self.datapsizer.AddGrowableRow(r,1)

		self.SetSizer(self.datapsizer)

		pub.subscribe(self.KlineWorkerHandler, "KlineWorker")

	def KlineWorkerHandler(self, info, value):
		if info == "hds":
			if value[0] in [0x10, 0x11, 0x17]:
				u = ">H12BHB"
				if value[0] == 0x11:
					u = ">H12BH2BH"
				data = struct.unpack(u, value[2][2:])
				self.enginespeedl.SetLabel("%d" % (data[0]))
				self.tpsensorl.SetLabel("%d" % (data[2]))
				self.ectsensorl.SetLabel("%d" % (-40 + data[4]))
				self.iatsensorl.SetLabel("%d" % (-40 + data[6]))
				self.mapsensorl.SetLabel("%d" % (data[8]))
				self.batteryvoltagel.SetLabel("%.03f" % (data[11]/10))
				self.vehiclespeedl.SetLabel("%d" % (data[12]))
				self.injectorl.SetLabel("%.03f" % (data[13]))
				self.advancel.SetLabel("%.01f" % (-64 + data[14]/255*127.5))
				if value[0] == 0x11:
					self.iacvpl.SetLabel("%d" % (data[15]))
					self.iacvcl.SetLabel("%.03f" % (data[16]/32767))
			elif value[0] in [0x20, 0x21]:
				if value[1] == 5:
					data = struct.unpack(">3B", value[2][2:])
					if value[0] == 0x20:
						self.o2volt1l.SetLabel("%.03f" % (data[0]/255*5))
						self.o2heat1l.SetLabel("Off" if data[2]==0 else "On")
						self.sttrim1l.SetLabel("%.03f" % (data[1]/255*2))
					else:
						self.o2volt2l.SetLabel("%.03f" % (data[0]/255*5))
						self.o2heat2l.SetLabel("Off" if data[2]==0 else "On")
						self.sttrim2l.SetLabel("%.03f" % (data[1]/255*2))
			elif value[0] == 0xd0:
				if value[1] > 2:
					data = struct.unpack(">7Bb%dB" % (value[1]-10), value[2][2:])
					self.egcvil.SetLabel("%.03f" % (data[5]/255*5))
					self.egcvtl.SetLabel("%.03f" % (data[6]/255*5))
					self.egcvll.SetLabel("%d" % (data[7]))
					self.lscl.SetLabel("%.03f" % (data[8]/255*1))
					self.lstl.SetLabel("%.03f" % (data[9]/255*1))
					self.lsvl.SetLabel("%d" % (data[10]))
			elif value[0] == 0xd1:
				if value[1] == 8:
					data = struct.unpack(">6B", value[2][2:])
					self.icsl.SetLabel("On" if data[0] else "Off")
			self.Layout()

class FlashPanel(wx.Panel):

	def __init__(self, parent):
		wx.Panel.__init__(self, parent)

		self.mode = wx.RadioBox(self, label="Mode", choices=["Read","Write","Recover"])
		self.wfilel = wx.StaticText(self, label="File")
		self.wchecksuml = wx.StaticText(self,label="Checksum")
		self.readfpicker = wx.FilePickerCtrl(self, wildcard="ECU dump (*.bin)|*.bin", style=wx.FLP_SAVE|wx.FLP_USE_TEXTCTRL|wx.FLP_SMALL)
		self.writefpicker = wx.FilePickerCtrl(self,wildcard="ECU dump (*.bin)|*.bin", style=wx.FLP_OPEN|wx.FLP_FILE_MUST_EXIST|wx.FLP_USE_TEXTCTRL|wx.FLP_SMALL)
		self.fixchecksum = wx.CheckBox(self, label="Fix")
		self.checksum = wx.Choice(self, choices=list(checksums))
		self.gobutton = wx.Button(self, label="Start")

		self.writefpicker.Show(False)
		self.fixchecksum.Show(False)
		self.checksum.Show(False)
		self.wchecksuml.Show(False)
		self.gobutton.Disable()
		self.checksum.Disable()

		self.optsbox = wx.BoxSizer(wx.HORIZONTAL)
		self.optsbox.Add(self.wchecksuml, 0, flag=wx.ALIGN_RIGHT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)
		self.optsbox.Add(self.checksum, 0)
		self.optsbox.Add(self.fixchecksum, 0, flag=wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)

		self.fpickerbox = wx.BoxSizer(wx.HORIZONTAL)
		self.fpickerbox.Add(self.readfpicker, 1)
		self.fpickerbox.Add(self.writefpicker, 1)

		self.flashpsizer = wx.GridBagSizer(0,0)
		self.flashpsizer.Add(self.mode, pos=(0,0), span=(1,6), flag=wx.ALL|wx.ALIGN_CENTER, border=20)
		self.flashpsizer.Add(self.wfilel, pos=(1,0), flag=wx.ALIGN_RIGHT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)
		self.flashpsizer.Add(self.fpickerbox, pos=(1,1), span=(1,5), flag=wx.EXPAND|wx.RIGHT, border=10)
		self.flashpsizer.Add(self.optsbox, pos=(2,0), span=(1,6), flag=wx.TOP, border=5)
		self.flashpsizer.Add(self.gobutton, pos=(4,5), flag=wx.ALIGN_RIGHT|wx.ALIGN_BOTTOM|wx.BOTTOM|wx.RIGHT, border=10)
		self.flashpsizer.AddGrowableRow(3,1)
		self.flashpsizer.AddGrowableCol(5,1)
		self.SetSizer(self.flashpsizer)

		self.fixchecksum.Bind(wx.EVT_CHECKBOX, self.OnFix)
		self.mode.Bind(wx.EVT_RADIOBOX, self.OnModeChange)
		self.gobutton.Bind(wx.EVT_BUTTON, self.OnGo)

	def OnFix(self, event):
		if self.fixchecksum.IsChecked():
			self.checksum.Enable()
		else:
			self.checksum.Disable()

	def OnModeChange(self, event):
		if self.mode.GetSelection() == 0:
			self.fixchecksum.Show(False)
			self.writefpicker.Show(False)
			self.readfpicker.Show(True)
			self.wchecksuml.Show(False)
			self.checksum.Show(False)
		else:
			self.wchecksuml.Show(True)
			self.checksum.Show(True)
			self.fixchecksum.Show(True)
			self.writefpicker.Show(True)
			self.readfpicker.Show(False)
		self.Layout()

	def OnGo(self, event):
		pass

	def setEmergency(self, emergency):
		if emergency:
			self.mode.EnableItem(0, False)
			self.mode.EnableItem(1, False)
			self.mode.EnableItem(2, True)
			self.mode.SetSelection(2)
		else:
			self.mode.EnableItem(0, True)
			self.mode.EnableItem(1, True)
			self.mode.EnableItem(2, True)

class HondaECU_GUI(wx.Frame):

	def __init__(self, args, version):
		# Initialize GUI things
		wx.Log.SetActiveTarget(wx.LogStderr())
		wx.Log.SetVerbose(args.verbose)
		if not args.debug:
			wx.Log.SetLogLevel(wx.LOG_Info)
		self.run = True
		self.active_device = None
		self.devices = {}
		title = "HondaECU %s" % (version)
		if getattr(sys, 'frozen', False):
			self.basepath = sys._MEIPASS
		else:
			self.basepath = os.path.dirname(os.path.realpath(__file__))
		ip = os.path.join(self.basepath,"honda.ico")

		# Initialize threads
		self.usbmonitor = USBMonitor(self)
		self.klineworker = KlineWorker(self)

		# Setup GUI
		wx.Frame.__init__(self, None, title=title)
		self.SetMinSize(wx.Size(800,600))
		ib = wx.IconBundle()
		ib.AddIcon(ip)
		self.SetIcons(ib)

		self.statusicons = [
			wx.Image(os.path.join(self.basepath, "bullet_black.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap(),
			wx.Image(os.path.join(self.basepath, "bullet_green.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap(),
			wx.Image(os.path.join(self.basepath, "bullet_yellow.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap(),
			wx.Image(os.path.join(self.basepath, "bullet_red.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap()
		]

		self.statusbar = ESB.EnhancedStatusBar(self, -1)
		self.statusbar.SetFieldsCount(4)
		self.statusbar.SetSize((-1, 32))
		self.SetStatusBar(self.statusbar)
		self.statusbar.SetStatusWidths([32, 170, 130, 110])
		self.statusbar.SetStatusStyles([wx.SB_SUNKEN, wx.SB_SUNKEN, wx.SB_SUNKEN, wx.SB_SUNKEN])

		self.statusicon = wx.StaticBitmap(self.statusbar)
		self.statusicon.SetBitmap(self.statusicons[0])
		self.ecmidl = wx.StaticText(self.statusbar)
		self.flashcountl = wx.StaticText(self.statusbar)
		self.dtccountl = wx.StaticText(self.statusbar)

		self.statusbar.AddWidget(self.statusicon, pos=0)
		self.statusbar.AddWidget(self.ecmidl, pos=1, horizontalalignment=ESB.ESB_ALIGN_LEFT)
		self.statusbar.AddWidget(self.flashcountl, pos=2, horizontalalignment=ESB.ESB_ALIGN_LEFT)
		self.statusbar.AddWidget(self.dtccountl, pos=3, horizontalalignment=ESB.ESB_ALIGN_LEFT)

		self.panel = wx.Panel(self)

		devicebox = wx.StaticBoxSizer(wx.HORIZONTAL, self.panel, "FTDI Devices")

		self.m_devices = wx.Choice(self.panel, wx.ID_ANY)
		devicebox.Add(self.m_devices, 1, wx.EXPAND | wx.ALL, 5)

		self.notebook = wx.Notebook(self.panel, wx.ID_ANY)
		self.flashp = FlashPanel(self.notebook)
		self.datap = DataPanel(self.notebook)
		self.errorp = ErrorPanel(self.notebook)
		self.notebook.AddPage(self.flashp, "Flash Operations")
		self.notebook.AddPage(self.datap, "Data Logging")
		self.notebook.AddPage(self.errorp, "Diagnostic Trouble Codes")

		mainbox = wx.BoxSizer(wx.VERTICAL)
		mainbox.Add(devicebox, 0, wx.EXPAND | wx.ALL, 10)
		mainbox.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)
		self.panel.SetSizer(mainbox)
		self.panel.Layout()

		# Bind event handlers
		self.Bind(wx.EVT_CLOSE, self.OnClose)
		self.m_devices.Bind(wx.EVT_CHOICE, self.OnDeviceSelected)
		pub.subscribe(self.USBMonitorHandler, "USBMonitor")
		pub.subscribe(self.KlineWorkerHandler, "KlineWorker")
		pub.subscribe(self.ErrorPanelHandler, "ErrorPanel")

		# Post GUI-setup actions
		self.Centre()
		self.Show()
		self.usbmonitor.start()
		self.klineworker.start()

	def __deactivate(self):
		self.active_device = None

	def OnClose(self, event):
		self.run = False
		self.usbmonitor.join()
		self.klineworker.join()
		for w in wx.GetTopLevelWindows():
			w.Destroy()

	def OnDeviceSelected(self, event):
		device = list(self.devices.keys())[self.m_devices.GetSelection()]
		serial = self.devices[device]
		if device != self.active_device:
			if self.active_device:
				pub.sendMessage("HondaECU.device", action="deactivate", device=self.active_device, serial=self.devices[self.active_device])
				self.__deactivate()
			if self.devices[device]:
				self.active_device = device
				pub.sendMessage("HondaECU.device", action="activate", device=self.active_device, serial=serial)
			else:
				pass

	def ErrorPanelHandler(self, action):
		if action == "cleardtc":
			self.dtccountl.SetLabel("   DTC Count: --")
			self.statusbar.OnSize(None)

	def USBMonitorHandler(self, action, device, serial):
		dirty = False
		if action == "add":
			wx.LogVerbose("Adding device (%s | %s)" % (device, serial))
			if not device in self.devices:
				self.devices[device] = serial
				dirty = True
		elif action =="remove":
			wx.LogVerbose("Removing device (%s | %s)" % (device, serial))
			if device in self.devices:
				if device == self.active_device:
					pub.sendMessage("HondaECU.device", action="deactivate", device=self.active_device, serial=self.devices[self.active_device])
					self.__deactivate()
				del self.devices[device]
				dirty = True
		if not self.active_device and len(self.devices) > 0:
			self.active_device = list(self.devices.keys())[0]
			pub.sendMessage("HondaECU.device", action="activate", device=self.active_device, serial=self.devices[self.active_device])
			dirty = True
		if dirty:
			self.m_devices.Clear()
			for device in self.devices:
				t = device
				if self.devices[device]:
					t += " | " + self.devices[device]
				self.m_devices.Append(t)
			if self.active_device:
				self.m_devices.SetSelection(list(self.devices.keys()).index(self.active_device))

	def KlineWorkerHandler(self, info, value):
		if info == "state":
			if value[0] in [0,12]:
				self.statusicon.SetBitmap(self.statusicons[0])
			elif value[0] in [1]:
				self.statusicon.SetBitmap(self.statusicons[1])
			elif value[0] in [10]:
				self.statusicon.SetBitmap(self.statusicons[3])
			else:
				self.statusicon.SetBitmap(self.statusicons[2])
			self.statusbar.OnSize(None)
		elif info == "ecmid":
			self.ecmidl.SetLabel("   ECM ID: %s" % value)
			self.statusbar.OnSize(None)
		elif info == "flashcount":
			self.flashcountl.SetLabel("   Flash Count: %d" % value)
			self.statusbar.OnSize(None)
		elif info == "dtccount":
			self.dtccountl.SetLabel("   DTC Count: %d" % value)
			if value > 0:
				self.errorp.resetbutton.Enable(True)
			else:
				self.errorp.resetbutton.Enable(False)
				self.errorp.errorlist.DeleteAllItems()
			self.statusbar.OnSize(None)
		elif info == "dtc":
			self.errorp.errorlist.DeleteAllItems()
			for code in value[hex(0x74)]:
				self.errorp.errorlist.Append([code, DTC[code] if code in DTC else "Unknown", "current"])
			for code in value[hex(0x73)]:
				self.errorp.errorlist.Append([code, DTC[code] if code in DTC else "Unknown", "past"])
			self.errorp.Layout()