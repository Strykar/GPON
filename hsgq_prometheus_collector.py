import argparse
from prometheus_client import start_http_server, Gauge, Info
import paramiko
import time
import re

# Define the command-line argument parser
parser = argparse.ArgumentParser(description='GPON Metrics Collector')
parser.add_argument('--hostname', action='append', help='Hostname or IP of the GPON device', required=True)
parser.add_argument('--port', action='append', type=int, help='SSH port of the GPON device', required=True)
parser.add_argument('--user', action='append', help='Username for SSH authentication', required=True)
parser.add_argument('--password', action='append', help='Password for SSH authentication', required=True)
parser.add_argument('--webserver-port', type=int, default=8114, help='Port for the Prometheus metrics web server to listen on')

# Parse command-line arguments
args = parser.parse_args()

# Metric Definitions
temperature_gauge = Gauge('gpon_temperature_celsius', 'Temperature of the GPON device in Celsius', ['ip'])
voltage_gauge = Gauge('gpon_voltage_volts', 'Voltage of the GPON device in Volts', ['ip'])
tx_power_gauge = Gauge('gpon_tx_power_dbm', 'Tx Power of the GPON device in dBm', ['ip'])
rx_power_gauge = Gauge('gpon_rx_power_dbm', 'Rx Power of the GPON device in dBm', ['ip'])
bias_current_gauge = Gauge('gpon_bias_current_mA', 'Bias Current of the GPON device in mA', ['ip'])
onu_state_gauge = Gauge('gpon_onu_state', 'ONU State of the GPON device', ['ip'])
onu_id_gauge = Gauge('gpon_onu_id', 'ONU ID of the GPON device', ['ip'])
loid_status_gauge = Gauge('gpon_loid_status', 'LOID Status of the GPON device', ['ip'])

# ONU State and LOID Status Mappings
onu_state_mapping = {
    '01': 1,
    '02': 2,        
    '03': 3,
    '04': 4,
    'O5': 5,
    '06': 6,
    '07': 7,    
    # Add more mappings for other states as needed
}

loid_status_mapping = {
    'Initial Status': 0,
    'Loid Error': 1,
    # Add more mappings for other statuses as needed
}

def fetch_and_update_metrics_via_ssh(hostname, port, username, password):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname, port, username, password, look_for_keys=False, allow_agent=False)

    commands = {
        'diag pon get transceiver bias-current': bias_current_gauge,
        'diag pon get transceiver rx-power': rx_power_gauge,
        'diag pon get transceiver temperature': temperature_gauge,
        'diag pon get transceiver tx-power': tx_power_gauge,
        'diag pon get transceiver voltage': voltage_gauge,
        'diag gpon get onu-state': onu_state_gauge,
        'omcicli get onuid': onu_id_gauge,
        'omcicli get state': loid_status_gauge,
    }

    for command, gauge in commands.items():
        stdin, stdout, stderr = client.exec_command(command)
        result = stdout.read().decode().strip()

        if command in ['diag pon get transceiver rx-power', 'diag pon get transceiver tx-power']:
            value = re.search(r'(-?\d+\.\d+)', result)
            if value:
                gauge.labels(ip=hostname).set(float(value.group(0)))
        elif command.startswith('diag gpon get onu-state'):
            state_code = re.search(r'ONU state: (.*)', result)
            if state_code:
                gauge.labels(ip=hostname).set(onu_state_mapping.get(state_code.group(1), 0))
        elif command.startswith('omcicli get state'):
            status_code = re.search(r'LOID Status: (.*)', result)
            if status_code:
                gauge.labels(ip=hostname).set(loid_status_mapping.get(status_code.group(1), 0))
        else:
            value = re.search(r'(\d+\.\d+)', result)
            if value:
                gauge.labels(ip=hostname).set(float(value.group(0)))

        stdin.close()
        stdout.close()
        stderr.close()

    client.close()

def main():
    start_http_server(args.webserver_port)
    while True:
        for i in range(len(args.hostname)):
            fetch_and_update_metrics_via_ssh(args.hostname[i], args.port[i], args.user[i], args.password[i])
        time.sleep(300)  # Fetch every 60 seconds

if __name__ == "__main__":
    main()
