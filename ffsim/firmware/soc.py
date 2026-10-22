# coding=utf-8
"""Layer (b): the STM32F407/F417 memory map, assembled from the peripheral models.

Addresses and IRQ numbers: RM0090 rev 19, table 1 (memory map) and table 61 (vector table).
BrennerLu's CubeMX export names the part STM32F417ZGTx; the F417 only adds the crypto/hash
blocks, which neither firmware is known to use, so the F407 map serves for both.
"""

from .cpu import Machine, Register
from .periph import Adc, Dma, Gpio, OtgFs, Pwr, Rcc, Spi, Timer, Usart
from .scs import Dwt, Scs

HCLK = 168e6
APB1_TIMER = 84e6  # APB1 = HCLK/4, timer clock x2 (RM0090 6.2)
APB2_TIMER = 168e6

# name: (base, irq, clock, bits[, cc_irq])
TIMERS = {
	"TIM1": (0x40010000, 25, APB2_TIMER, 16, 27),
	"TIM2": (0x40000000, 28, APB1_TIMER, 32),
	"TIM3": (0x40000400, 29, APB1_TIMER, 16),
	"TIM4": (0x40000800, 30, APB1_TIMER, 16),
	"TIM5": (0x40000C00, 50, APB1_TIMER, 32),
	"TIM6": (0x40001000, 54, APB1_TIMER, 16),
	"TIM7": (0x40001400, 55, APB1_TIMER, 16),
	"TIM8": (0x40010400, 44, APB2_TIMER, 16, 46),
	"TIM9": (0x40014000, 24, APB2_TIMER, 16),
	"TIM10": (0x40014400, 25, APB2_TIMER, 16),
	"TIM11": (0x40014800, 26, APB2_TIMER, 16),
	"TIM12": (0x40001800, 43, APB1_TIMER, 16),
	"TIM13": (0x40001C00, 44, APB1_TIMER, 16),
	"TIM14": (0x40002000, 45, APB1_TIMER, 16),
}
SPIS = {"SPI1": 0x40013000, "SPI2": 0x40003800, "SPI3": 0x40003C00}
USARTS = {"USART1": 0x40011000, "USART2": 0x40004400, "USART3": 0x40004800, "UART4": 0x40004C00,
		  "UART5": 0x40005000, "USART6": 0x40011400}


class Stm32f4(object):
	def __init__(self, cpi=1.0):
		m = self.m = Machine(hz=HCLK, cpi=cpi)
		m.map_bus(0x40000000, 0x80000)
		m.map_bus(0x50000000, 0x61000)
		m.map_bus(0xA0000000, 0x1000)
		m.map_bus(0xE0000000, 0x100000)
		m.map_bitband(0x42000000, 0x40000000, 0x80000)  # peripheral bit-band
		m.map_bitband(0x22000000, 0x20000000, 0x20000)  # SRAM bit-band
		# FSMC banks (LCD, external memories): plain memory so pixel writes cost nothing
		m.uc.mem_map(0x60000000, 0x01000000)
		m.uc.mem_map(0x64000000, 0x01000000)
		m.uc.mem_map(0x68000000, 0x01000000)
		m.uc.mem_map(0x6C000000, 0x01000000)

		self.scs = Scs(m)
		self.nvic = self.scs.nvic
		m.attach(0xE000E000, 0x1000, self.scs)
		m.attach(0xE0001000, 0x1000, Dwt(m))
		self.rcc = Rcc()
		m.attach(0x40023800, 0x400, self.rcc)
		m.attach(0x40007000, 0x400, Pwr("PWR"))
		self.bkpsram = Register("BKPSRAM")  # 4 KB battery-backed SRAM, starts zeroed
		m.attach(0x40024000, 0x1000, self.bkpsram)
		self.gpio = {}
		for i, port in enumerate("ABCDEFGHI"):
			self.gpio[port] = Gpio(m, "GPIO" + port)
			m.attach(0x40020000 + 0x400 * i, 0x400, self.gpio[port])
		self.tim = {}
		for name, spec in TIMERS.items():
			base, irq, clk, bits = spec[:4]
			cc = spec[4] if len(spec) > 4 else None
			self.tim[name] = Timer(m, self.nvic, name, irq, clk, bits, cc)
			m.attach(base, 0x400, self.tim[name])
		self.adc = Adc(m, self.nvic)
		m.attach(0x40012000, 0x100, self.adc)
		self.dma1, self.dma2 = Dma("DMA1"), Dma("DMA2")
		m.attach(0x40026000, 0x400, self.dma1)
		m.attach(0x40026400, 0x400, self.dma2)
		self.adc.dma = lambda: self.dma2.stream(0)  # ADC1 -> DMA2 stream 0 (RM0090 table 43)
		self.spi = {}
		for name, base in SPIS.items():
			self.spi[name] = Spi(m, name)
			m.attach(base, 0x400, self.spi[name])
		for name, base in USARTS.items():
			m.attach(base, 0x400, Usart(name))
		self.otg_fs = OtgFs("OTG_FS")
		m.attach(0x50000000, 0x40000, self.otg_fs)
		self.fallback = Register("unmodelled")
		m.attach(0x40000000, 0x80000, self.fallback)  # catch-all for unmodelled APB/AHB1 blocks
		m.attach(0x50000000, 0x61000, Register("AHB2"))
		m.attach(0xA0000000, 0x1000, Register("FSMC"))

	def pin(self, name):
		"""'PC10' -> (Gpio, 10)"""
		return self.gpio[name[1]], int(name[2:])
