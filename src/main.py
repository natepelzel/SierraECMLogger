"""
SierraECMLogger — main entry point.

Connects to an Arduino over serial and reads ECM data from a GMC Sierra turbo.
"""

import serial


def main():
    # TODO: configure port and baud rate
    port = "/dev/ttyUSB0"
    baud_rate = 9600

    print(f"Connecting to Arduino on {port} at {baud_rate} baud...")

    with serial.Serial(port, baud_rate, timeout=1) as ser:
        print("Connected. Reading ECM data (Ctrl+C to stop)...")
        try:
            while True:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if line:
                    print(line)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
