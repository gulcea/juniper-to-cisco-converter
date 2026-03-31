# Juniper to Cisco Configuration Converter

## 📌 Overview
A Python-based tool that converts Juniper network configurations into Cisco-compatible configurations automatically.


It parses Juniper CLI-style configuration lines and generates equivalent Cisco configuration output.

---

## 🚀 Features

- 🔄 Convert Juniper interface names to Cisco format
- 🌐 VLAN detection and mapping
- 🔌 Access & trunk port conversion
- 🧠 Automatic handling of undefined VLANs
- 📡 SVI (Layer 3 VLAN interface) support
- 🛣️ Static route conversion
- 🔐 SNMP configuration support
- 📶 LLDP & IGMP support
- ⚡ Storm control generation
- 🧾 Debug JSON output for analysis

---

## 📦 Installation

```bash
git clone https://github.com/gulcea/juniper-to-cisco-converter.git
cd juniper-to-cisco-converter
