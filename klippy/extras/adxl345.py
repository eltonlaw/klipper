# Support for reading acceleration data from an adxl345 chip
#
# Copyright (C) 2020  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, time
from . import bus

QUERY_RATES = {
    25: 0x8, 50: 0x9, 100: 0xa, 200: 0xb, 400: 0xc,
    800: 0xd, 1600: 0xe, 3200: 0xf,
}

SCALE = 0.004 * 9.80665 * 1000 # 4mg/LSB * Earth gravity in mm/s**2

REG_DEVID = 0x00
REG_BW_RATE = 0x2C
REG_DATA_FORMAT = 0x31
REG_FIFO_CTL = 0x38
REG_MOD_READ = 0x80
REG_MOD_MULTI = 0x40

class ADXL345:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.query_rate = 0
        self.last_tx_time = 0.
        # Measurement storage (accessed from background thread)
        self.samples = []
        self.last_sequence = 0
        self.samples_start1 = self.samples_start2 = 0.
        # Setup mcu sensor_adxl345 bulk query code
        self.spi = bus.MCU_SPI_from_config(config, 3, default_speed=5000000)
        self.mcu = mcu = self.spi.get_mcu()
        self.oid = oid = mcu.create_oid()
        self.query_adxl345_cmd = self.query_adxl345_end_cmd =None
        mcu.add_config_cmd("config_adxl345 oid=%d spi_oid=%d"
                           % (oid, self.spi.get_oid()))
        mcu.add_config_cmd("query_adxl345 oid=%d clock=0 rest_ticks=0"
                           % (oid,), on_restart=True)
        mcu.register_config_callback(self._build_config)
        mcu.register_response(self._handle_adxl345_start, "adxl345_start", oid)
        mcu.register_response(self._handle_adxl345_data, "adxl345_data", oid)
        # Register commands
        name = config.get_name().split()[1]
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("ACCELEROMETER_MEASURE", "CHIP", name,
                                   self.cmd_ACCELEROMETER_MEASURE,
                                   desc=self.cmd_ACCELEROMETER_MEASURE_help)
    def _build_config(self):
        self.query_adxl345_cmd = self.mcu.lookup_command(
            "query_adxl345 oid=%c clock=%u rest_ticks=%u",
            cq=self.spi.get_command_queue())
        self.query_adxl345_end_cmd = self.mcu.lookup_query_command(
            "query_adxl345 oid=%c clock=%u rest_ticks=%u",
            "adxl345_end oid=%c end1_time=%u end2_time=%u"
            " limit_count=%hu sequence=%hu",
            oid=self.oid, cq=self.spi.get_command_queue())
    def _clock_to_print_time(self, clock):
        return self.mcu.clock_to_print_time(self.mcu.clock32_to_clock64(clock))
    def _handle_adxl345_start(self, params):
        self.samples_start1 = self._clock_to_print_time(params['start1_time'])
        self.samples_start2 = self._clock_to_print_time(params['start2_time'])
    def _handle_adxl345_data(self, params):
        last_sequence = self.last_sequence
        sequence = (last_sequence & ~0xffff) | params['sequence']
        if sequence < last_sequence:
            sequence += 0x10000
        self.last_sequence = sequence
        samples = self.samples
        if len(samples) >= 200000:
            # Avoid filling up memory with too many samples
            return
        samples.append((sequence, params['data']))
    def _convert_sequence(self, sequence):
        sequence = (self.last_sequence & ~0xffff) | sequence
        if sequence < self.last_sequence:
            sequence += 0x10000
        return sequence
    def do_start(self, rate):
        # Verify chip connectivity
        params = self.spi.spi_transfer([REG_DEVID | REG_MOD_READ, 0x00])
        response = bytearray(params['response'])
        if response[1] != 0xe5:
            raise self.printer.command_error("Invalid adxl345 id (got %x vs %x)"
                                             % (response[1], 0xe5))
        # Setup chip in requested query rate
        clock = 0
        if self.last_tx_time:
            clock = self.mcu.print_time_to_clock(self.last_tx_time)
        self.spi.spi_send([REG_DATA_FORMAT, 0x0B], minclock=clock)
        self.spi.spi_send([REG_FIFO_CTL, 0x80])
        self.spi.spi_send([REG_BW_RATE, QUERY_RATES[rate]])
        # Setup samples
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        self.samples = []
        self.last_sequence = 0
        self.samples_start1 = self.samples_start2 = print_time
        # Start bulk reading
        reqclock = self.mcu.print_time_to_clock(print_time)
        rest_ticks = self.mcu.seconds_to_clock(4. / rate)
        self.last_tx_time = print_time
        self.query_rate = rate
        self.query_adxl345_cmd.send([self.oid, reqclock, rest_ticks],
                                    reqclock=reqclock)
    def do_end(self):
        query_rate = self.query_rate
        if not query_rate:
            return
        # Halt bulk reading
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        clock = self.mcu.print_time_to_clock(print_time)
        params = self.query_adxl345_end_cmd.send([self.oid, 0, 0],
                                                 minclock=clock)
        self.last_tx_time = print_time
        self.query_rate = 0
        samples = self.samples
        self.samples = []
        # Report results
        end1_time = self._clock_to_print_time(params['end1_time'])
        end2_time = self._clock_to_print_time(params['end2_time'])
        end_sequence = self._convert_sequence(params['sequence'])
        total_count = (end_sequence - 1) * 8 + len(samples[-1][1]) // 6
        start2_time = self.samples_start2
        total_time = end2_time - start2_time
        sample_to_time = total_time / total_count
        seq_to_time = sample_to_time * 8.
        fname = "/tmp/adxl345-%s.csv" % (time.strftime("%Y%m%d_%H%M%S"),)
        f = open(fname, "w")
        f.write("##start=%.6f/%.6f,end=%.6f/%.6f\n"
                % (self.samples_start1, start2_time, end1_time, end2_time))
        f.write("##limit_count=%d,end_seq=%d,time_per_sample=%.9f\n"
                % (params['limit_count'], end_sequence, sample_to_time))
        f.write("#time,x,y,z\n")
        actual_count = 0
        for i in range(len(samples)):
            seq, data = samples[i]
            d = bytearray(data)
            count = len(data)
            sdata = [((d[j*2] | (d[j*2+1] << 8))
                      - ((d[j*2+1] & 0x80) << 9)) * SCALE
                     for j in range(count//2)]
            seq_time = start2_time + seq * seq_to_time
            for j in range(count//6):
                f.write("%.6f,%.6f,%.6f,%.6f\n"
                        % (seq_time + j * sample_to_time,
                           sdata[j*3], sdata[j*3 + 1], sdata[j*3 + 2]))
                actual_count += 1
        f.write("##count=%d/%d,drops=%d"
                % (total_count, actual_count, total_count - actual_count))
        f.close()
    cmd_ACCELEROMETER_MEASURE_help = "Start/stop accelerometer"
    def cmd_ACCELEROMETER_MEASURE(self, gcmd):
        rate = gcmd.get_int("RATE", 0)
        if not rate:
            self.do_end()
            gcmd.respond_info("adxl345 measurements stopped")
        elif self.query_rate:
            raise gcmd.error("adxl345 already running")
        elif rate not in QUERY_RATES:
            raise gcmd.error("Not a valid adxl345 query rate")
        else:
            self.do_start(rate)

def load_config_prefix(config):
    return ADXL345(config)
