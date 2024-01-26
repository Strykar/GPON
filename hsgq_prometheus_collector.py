from prometheus_client import start_http_server, Gauge
import time
import requests
from bs4 import BeautifulSoup

# GPON Metrics
temperature_gauge = Gauge('gpon_temperature_celsius', 'Temperature of the GPON device in Celsius')
voltage_gauge = Gauge('gpon_voltage_volts', 'Voltage of the GPON device in Volts')
tx_power_gauge = Gauge('gpon_tx_power_dbm', 'Tx Power of the GPON device in dBm')
rx_power_gauge = Gauge('gpon_rx_power_dbm', 'Rx Power of the GPON device in dBm')
bias_current_gauge = Gauge('gpon_bias_current_mA', 'Bias Current of the GPON device in mA')
onu_state_gauge = Gauge('gpon_onu_state', 'ONU State of the GPON device')
onu_id_gauge = Gauge('gpon_onu_id', 'ONU ID of the GPON device')
loid_status_gauge = Gauge('gpon_loid_status', 'LOID Status of the GPON device')

# VLAN Metrics
vlan_cfg_type_gauge = Gauge('vlan_config_type', 'VLAN Configuration Type (0=Auto, 1=Manual)')
vlan_mode_gauge = Gauge('vlan_mode', 'VLAN Mode (0=Transparent, 1=PVID)')
vlan_number_gauge = Gauge('vlan_number', 'VLAN number')

# System Metrics
cpu_usage_gauge = Gauge('cpu_usage_percent', 'CPU Usage of the device in percent')
memory_usage_gauge = Gauge('memory_usage_percent', 'Memory Usage of the device in percent')

# ONU State Mapping
onu_state_mapping = {
    '03': 3,  # Adjust based on actual values and meanings
    'O5': 5,  # Example mapping
    # Add more mappings for other states
}

# LOID Status Mapping
loid_status_mapping = {
    'Initial Status': 0,
    'Loid Error': 1,
    # Add more mappings for other statuses
}

def numeric_ip(ip):
    """Convert dotted IP address to a numeric representation."""
    return sum(int(part) << (8 * i) for i, part in enumerate(reversed(ip.split('.'))))

def parse_metric_value(row):
    return row.find_all('td')[1].text.strip().split()[0]

def fetch_and_update_gpon_metrics():
    url = "http://192.168.1.1/status_pon.asp"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')

    rows = soup.find_all('tr', bgcolor="#DDDDDD")

    temperature_gauge.set(float(parse_metric_value(rows[0])))
    voltage_gauge.set(float(parse_metric_value(rows[1])))
    tx_power_gauge.set(float(parse_metric_value(rows[2])))
    rx_power_gauge.set(float(parse_metric_value(rows[3])))
    bias_current_gauge.set(float(parse_metric_value(rows[4])))

    gpon_status_rows = rows[5:8]
    onu_state_raw = parse_metric_value(gpon_status_rows[0])
    onu_state_value = onu_state_mapping.get(onu_state_raw, 0)  # Default to 0 if unknown
    onu_state_gauge.set(onu_state_value)

    onu_id_gauge.set(float(parse_metric_value(gpon_status_rows[1])))
    
    loid_status_raw = parse_metric_value(gpon_status_rows[2])
    loid_status_value = loid_status_mapping.get(loid_status_raw, 0)  # Default to 0 if unknown
    loid_status_gauge.set(loid_status_value)

def fetch_and_update_vlan_metrics():
    url = "http://192.168.1.1/vlan.asp"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')

    vlan_cfg_type = soup.find('input', {'name': 'vlan_cfg_type', 'checked': True})
    vlan_cfg_type_value = 0 if vlan_cfg_type and vlan_cfg_type['value'] == '0' else 1
    vlan_cfg_type_gauge.set(vlan_cfg_type_value)

    vlan_mode = soup.find('input', {'name': 'vlan_manu_mode', 'checked': True})
    vlan_mode_value = 0 if vlan_mode and vlan_mode['value'] == '0' else 1
    vlan_mode_gauge.set(vlan_mode_value)

    vlan_number_input = soup.find('input', {'name': 'vlan_manu_tag_vid'})
    vlan_number = int(vlan_number_input['value']) if vlan_number_input and vlan_number_input['value'] else 0
    vlan_number_gauge.set(vlan_number)

def fetch_and_update_system_metrics():
    url = "http://192.168.1.1/status.asp"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'html.parser')

    cpu_usage = int(soup.find(string='CPU Usage').find_next('td').text.strip().strip('%'))
    memory_usage = int(soup.find(string='Memory Usage').find_next('td').text.strip().strip('%'))

    cpu_usage_gauge.set(cpu_usage)
    memory_usage_gauge.set(memory_usage)

def main():
    start_http_server(8000)
    while True:
        fetch_and_update_gpon_metrics()
        fetch_and_update_vlan_metrics()
        fetch_and_update_system_metrics()
        time.sleep(300)

if __name__ == "__main__":
    main()
