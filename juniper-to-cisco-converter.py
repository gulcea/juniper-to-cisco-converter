# Juniper to Cisco configuration converter 

import re
import os
import json


# ---------------------------------------------------------------------
#  Interface Name Convert (Juniper → Cisco)
# ---------------------------------------------------------------------
def convert_interface_name(juniper_if: str) -> str:
    # Juniper'deki "ge-0/0/22" formatını yakalayıp,
    # sadece port numarasını alıyoruz (22) ve Cisco tarzı isme çeviriyoruz.
    m = re.match(r"ge-(\d+)/(\d+)/(\d+)", juniper_if)
    if m:
        return f"GigabitEthernet0/{m.group(3)}"
    # Eğer beklediğimiz formatta değilse, dokunmadan geri veriyoruz.
    return juniper_if # Eğer map'te yoksa → undefined VLAN

# ---------------------------------------------------------------------
#  CIDR → Netmask
# ---------------------------------------------------------------------
def cidr_to_mask(cidr: str) -> str:
    # Örn: "/24" bilgisini alıp "255.255.255.0" gibi klasik maske formatına çeviriyoruz.
    cidr_int = int(cidr)
    mask = (0xffffffff << (32 - cidr_int)) & 0xffffffff
    return ".".join(str((mask >> (8 * i)) & 0xff) for i in range(3, -1, -1))

# ---------------------------------------------------------------------
#  VLAN Resolver 
#  Bilinmeyen VLAN isimleri → "undefined"
# ---------------------------------------------------------------------
def resolve_vlan(token: str, vlan_map: dict) -> str:
    t = token.lower()

    # Eğer direkt sayı ise VLAN ID olarak al
    if t.isdigit():
        return t

    # vlan100 → 100
    if t.startswith("vlan") and t[4:].isdigit():
        return t[4:]

    # Eğer map'te yoksa → undefined VLAN
    # Yani: Juniper'de adı geçiyor ama ID'si tanımlı değilse, biz bunu bilerek "undefined" diye işaretliyoruz.(vlan all yerine.)
    return vlan_map.get(t, "undefined")


# ---------------------------------------------------------------------
#  convert_config – ANA DÖNÜŞTÜRÜCÜ
# ---------------------------------------------------------------------
def convert_config(juniper_lines):
    # Temel toplayıcı değişkenler: hostname, SNMP, statik route, VLAN ve interface bilgileri
    hostname = None
    snmp = []
    routes = []

    vlan_name_to_id = {}
    vlan_id_to_name = {}
    svi_vlans = set()
    svi_ips = {}
    interfaces = {}
    used_vlans = set()

    igmp = False
    lldp = False
    storm = False

    # ------------------------------------------------------------
    # 1) VLAN TANIMLARI
    # ------------------------------------------------------------
    # Bu ilk döngüde sadece "set vlans ... vlan-id ..." satırlarını okuyup,
    # VLAN adı ↔ VLAN ID eşlemesini çıkarıyoruz.
    for raw in juniper_lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith("set vlans") and "vlan-id" in line:
            parts = line.split()
            name = parts[2]
            raw_vid = parts[-1]

            # vlan100 → 100
            if raw_vid.lower().startswith("vlan") and raw_vid[4:].isdigit():
                vid = raw_vid[4:]
            else:
                vid = raw_vid

            vlan_name_to_id[name.lower()] = vid
            vlan_id_to_name[vid] = name

    # ------------------------------------------------------------
    # 2) DİĞER AYARLAR
    # ------------------------------------------------------------
    # Bu ikinci büyük döngü; hostname, SNMP, route, SVI ve fiziksel portların
    # tamamını okuyup iç veri yapılarımızı dolduruyor.
    for raw in juniper_lines:
        line = raw.strip()
        if not line:
            continue

        # Hostname
        if line.startswith("set system host-name"):
            hostname = "hostname " + line.split()[-1]
            continue

        # SNMP
        if line.startswith("set snmp community"):
            parts = line.split()
            snmp.append(f"snmp-server community {parts[3]} RO")
            continue

        # Default route
        if "static route" in line and "next-hop" in line:
            parts = line.split()
            routes.append(f"ip route 0.0.0.0 0.0.0.0 {parts[-1]}")
            continue

        # L3 VLAN (SVI)
        if line.startswith("set vlans") and "l3-interface vlan." in line:
            vid = line.split("vlan.")[-1]
            svi_vlans.add(vid)
            continue

        if line.startswith("set interfaces vlan unit") and "family inet address" in line:
            # Burada VLAN interface'ine IP / mask bilgisini çekiyoruz (SVI için).
            parts = line.split()
            vid = parts[4]
            ip, cidr = parts[-1].split("/")
            svi_ips[vid] = (ip, cidr_to_mask(cidr))
            svi_vlans.add(vid)
            continue

        # --------------------------------------------------------
        # Fiziksel interface işlemleri
        # --------------------------------------------------------
        if line.startswith("set interfaces ge"):
            parts = line.split()
            jun_if = parts[2]
            cif = convert_interface_name(jun_if)

            # Her Cisco interface için tek bir sözlükte tüm bilgileri topluyoruz.
            if cif not in interfaces:
                interfaces[cif] = {
                    "description": None,
                    "mode": None,
                    "access_vlan": None,
                    "native_vlan": None,
                    "trunk_all": False,
                    "trunk_allowed": set(),
                    "l3_ip": None,
                    "l3_mask": None
                }

            iface = interfaces[cif]

            # L3 interface (routed port)
            # Yani bu port switchport değil, router portu gibi kullanılacak.
            if "family inet address" in line:
                ip_cidr = parts[-1]
                ip, cidr = ip_cidr.split("/")
                iface["l3_ip"] = ip
                iface["l3_mask"] = cidr_to_mask(cidr)
                iface["mode"] = "l3"
                continue

            # Description
            if "description" in line:
                idx = parts.index("description")
                iface["description"] = " ".join(parts[idx + 1:])
                continue

            # MODE: access
            if "port-mode access" in line:
                iface["mode"] = "access"
                continue

            # MODE: trunk
            if "port-mode trunk" in line:
                iface["mode"] = "trunk"
                continue

            # Native VLAN
            if "native-vlan-id" in line:
                # Native VLAN'ı, isim/verilen değer ne olursa olsun resolve_vlan ile ID'ye çeviriyoruz.
                vid = resolve_vlan(parts[-1], vlan_name_to_id)
                iface["native_vlan"] = vid
                used_vlans.add(vid)
                continue

            # VLAN MEMBERS (PATCH EDİLMİŞ KISIM)
            if "vlan members" in line:
                token = parts[-1]

                # "all"
                # Trunk portta "all" gelirse, Cisco tarafında da "allowed vlan all" yazacağız.
                if token == "all":
                    iface["trunk_all"] = True
                else:
                    vid = resolve_vlan(token, vlan_name_to_id)

                    # ============================
                    #   UNKNOWN VLAN → UNDEFINED
                    # ============================
                    # Buradaki kritik nokta:
                    # Juniper'de adı geçen ama ID'si tanımlanmamış VLAN'ları
                    # "undefined" diye işaretliyoruz ki config'te gözden kaçmasın.
                    if vid == "undefined":
                        if iface["mode"] == "trunk":
                            iface["trunk_allowed"].add("undefined")
                        else:
                            iface["access_vlan"] = "undefined"
                        continue

                    # Normal VLAN
                    used_vlans.add(vid)

                    if iface["mode"] == "trunk":
                        iface["trunk_allowed"].add(vid)
                    else:
                        iface["access_vlan"] = vid

                continue

            continue  # interface block sonu

        # IGMP
        if line.startswith("set protocols igmp-snooping"):
            igmp = True
            continue

        # LLDP
        if line.startswith("set protocols lldp"):
            lldp = True
            continue

        # Storm Control
        if line.startswith("set ethernet-switching-options storm-control"):
            # Burada sadece "storm-control" özelliğinin açıldığını not ediyoruz,
            # detaylı seviyeyi Cisco tarafında aşağıda sabit bir template ile yazıyoruz.
            storm = True
            continue

    # =============================================================
    # Kullanılan VLAN'ları tamamlama
    # =============================================================
    # 1) Fiziksel portlarda kullanılan VLAN ID'leri için eksik isimleri doldur
    for vid in used_vlans:
        if vid.isdigit() and vid not in vlan_id_to_name:
            vlan_id_to_name[vid] = f"VLAN{vid}"

    # 2) Sadece SVI olarak geçen (ör: Vlan80) ama hiç "set vlans" ile tanımlanmamış
    #    VLAN'lar varsa onlar için de otomatik bir isim üret:
    #    vlan 80
    #     name VLAN80
    for vid in svi_vlans:
        if vid.isdigit() and vid not in vlan_id_to_name:
            vlan_id_to_name[vid] = f"VLAN{vid}"


    # =============================================================
    #  ÇIKTI OLUŞTURMA
    # =============================================================
    out = []

    # Hostname
    if hostname:
        out.append(hostname)
    out.append("")

    # VLAN'lar
    for vid in sorted(vlan_id_to_name.keys(), key=lambda x: int(x)):
        out.append(f"vlan {vid}")
        out.append(f" name {vlan_id_to_name[vid]}")
        out.append("")

    # SVI'lar
    # Burada Layer 3 VLAN interface'lerini (interface VlanX) üretiyoruz.
    for vid in sorted(svi_vlans, key=lambda x: int(x)):
        out.append(f"interface Vlan{vid}")
        if vid in svi_ips:
            ip, mask = svi_ips[vid]
            out.append(f" ip address {ip} {mask}")
        out.append(" no shutdown")
        out.append("")

    # ------------------------------------------------------------
    # Fiziksel portları sıralama
    # ------------------------------------------------------------
    def if_sort_key(name):

        return int(name.split("/")[-1])

    for cif in sorted(interfaces.keys(), key=if_sort_key):
        data = interfaces[cif]

        # Eğer mod belirtilmemişse otomatik tespit et
        # Yani; access VLAN varsa access, trunk işaretleri varsa trunk kabul ediyoruz.
        if data["mode"] is None:
            if data["access_vlan"]:
                data["mode"] = "access"
            elif data["trunk_all"] or data["trunk_allowed"] or data["native_vlan"]:
                data["mode"] = "trunk"

        out.append(f"interface {cif}")

        # Description
        if data["description"]:
            out.append(f" description {data['description']}")

        # ACCESS MODE + STP HARDENING
        if data["mode"] == "access":
            out.append(" switchport mode access")
            out.append(f" switchport access vlan {data['access_vlan']}")

            # STP ile ilgili ek güvenlik ayarları (opsiyonel):
        # Bu satırlar aktif edildiğinde, access portlar için:
        #   - "spanning-tree portfast"
        #   - "spanning-tree bpduguard enable"
        # komutlarını üretir ve Layer 2 tarafında ek koruma sağlar.
        #  Kaynak Juniper konfigürasyonunda bu ayarların doğrudan bir
        # karşılığı belirtilmediği için, converter'ı varsayılan durumda
        # bu satırları çalıştırmaması için ayarladım.
        # 
        # out.append(" spanning-tree portfast")
        # out.append(" spanning-tree bpduguard enable")

        # TRUNK MODE
        elif data["mode"] == "trunk":
            out.append(" switchport trunk encapsulation dot1q")
            out.append(" switchport mode trunk")

            # Eğer Juniper'de "all" denmişse, Cisco'da da "allowed vlan all" yazıyoruz.
            if data["trunk_all"]:
                out.append(" switchport trunk allowed vlan all")
            else:
                # Aksi halde, spesifik VLAN listesi virgülle ayrılmış şekilde yazılıyor.
                if data["trunk_allowed"]:
                    allowed = list(data["trunk_allowed"])
                    out.append(f" switchport trunk allowed vlan {','.join(allowed)}")

            if data["native_vlan"]:
                out.append(f" switchport trunk native vlan {data['native_vlan']}")

        # ROUTED PORT
        elif data["mode"] == "l3":
            out.append(" no switchport")
            out.append(f" ip address {data['l3_ip']} {data['l3_mask']}")

        else:
            # Modu hiç belirlenememiş çok edge-case durumlarda,
            # en azından "switchport" diyerek Layer 2 port olduğunu söylüyoruz.
            out.append(" switchport")

        out.append("")

    # ------------------------------------------------------------
    # Diğer ayarlar
    # ------------------------------------------------------------
    # Statik rotalar, SNMP community, LLDP, IGMP ve storm-control gibi
    # global ayarları en sona ekliyoruz.
    if routes:
        out.extend(routes)
    if snmp:
        out.extend(snmp)
    if lldp:
        out.append("lldp run")
    if igmp:
        out.append("ip igmp snooping")
    if storm:
        # Storm-control'u tüm access port aralığı için tek satırla veriyoruz.
        out.append("interface range GigabitEthernet0/0 - 0/23")
        out.append(" storm-control broadcast level 10.00")
        out.append(" storm-control multicast level 10.00")

    out.append("end")

    # ------------------------------------------------------------
    # DEBUG JSON
    # ------------------------------------------------------------
    # Buradaki JSON çıktısı; dönüştürülen yapıları dışarı atıp,
    # sonradan "ne olmuş, hangi VLAN nasıl eşleşmiş" diye bakabilmek için.
    debug_json = {
        "vlan_name_to_id": vlan_name_to_id,
        "vlan_id_to_name": vlan_id_to_name,
        "interfaces": {
            k: {
                kk: (list(v) if isinstance(v, set) else v)
                for kk, v in d.items()
            } for k, d in interfaces.items()
        },
        "svi_ips": svi_ips,
        "svi_vlans": list(svi_vlans),
        "used_vlans": list(used_vlans)
    }

    return "\n".join(out), debug_json

# ---------------------------------------------------------------------
#  MAIN APPLICATION
# ---------------------------------------------------------------------

print("""
Girdi alma yöntemi:
1 - Juniper komutlarını tek tek gireceğim
2 - Juniper config dosyasından okuyacağım
""")

choice = input("Seçim: ").strip()

# ---------------------------------------------------------------------
# 1) MANUEL KOMUT GİRİŞİ
# ---------------------------------------------------------------------
if choice == "1":
    # Kullanıcı tek tek Juniper satırlarını konsoldan yapıştırıp dönüştürebilsin diye.
    print("Komutları gir: (bitirmek için boş satır)")
    lines = []
    while True:
        l = input()
        if l.strip() == "":
            break
        lines.append(l)

    cisco, debug_json = convert_config(lines)

    print("\n--- Cisco Output ---\n")
    print(cisco)

    # İsterse debug JSON'u da diske kaydediyoruz.
    save = input("\nJSON çıktı kaydedilsin mi? (E/H): ").strip().lower()
    if save == "e":
        with open("conversion_debug.json", "w") as f:
            json.dump(debug_json, f, indent=4)
        print("JSON kaydedildi: conversion_debug.json")

# ---------------------------------------------------------------------
# 2) DOSYADAN OKUMA
# ---------------------------------------------------------------------
elif choice == "2":
    # Burada ise doğrudan bir Juniper config dosyasını okuyup,
    # aynı klasöre "_converted.conf" uzantılı Cisco dosyası üretıyoruz.
    filename = input("Dosya adı: ").strip().replace('"', "").replace("'", "")

    if not os.path.exists(filename):
        print("\nHATA: Dosya bulunamadı!")
    else:
        with open(filename) as f:
            jun_lines = f.read().splitlines()

        cisco, debug_json = convert_config(jun_lines)

        outname = os.path.splitext(filename)[0] + "_converted.conf"
        with open(outname, "w") as f:
            f.write(cisco)

        print(f"\nDönüştürme tamamlandı! Çıktı: {outname}")

        save = input("\nJSON çıktı kaydedilsin mi? (E/H): ").strip().lower()
        if save == "e":
            with open("conversion_debug.json", "w") as f:
                json.dump(debug_json, f, indent=4)
            print("JSON kaydedildi: conversion_debug.json")


else:
    # Kullanıcı 1 veya 2 dışında bir şey yazarsa basit hata mesajı.
    print("Hatalı seçim yaptınız.Lütfen 1 veya 2 giriniz.")
