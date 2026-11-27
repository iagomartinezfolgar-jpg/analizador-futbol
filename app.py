import csv
from datetime import datetime, timedelta
import difflib
import io
import math
import unicodedata
import requests
import streamlit as st

# ==========================================
# CONFIGURACIÓN DE PÁGINA (DISEÑO MÓVIL)
# ==========================================
st.set_page_config(
    page_title="Radar Fútbol EV+ v2",
    page_icon="⚽",
    layout="centered",
)

try:
    ODDS_API_KEY = st.secrets["ODDS_API_KEY"].strip()
    FOOTBALL_DATA_KEY = st.secrets["FOOTBALL_DATA_KEY"].strip()
except Exception:
    st.error(
        "⚠️ Faltan las claves API. Ve a Settings -> Secrets en Streamlit Cloud y añade ODDS_API_KEY y FOOTBALL_DATA_KEY."
    )
    st.stop()

PRIORIDAD_CASAS = ["winamax_fr", "winamax_de", "bet365", "pinnacle", "unibet_eu"]

ALIAS_EQUIPOS = {
    "Ath Madrid": "Atletico Madrid",
    "Ath Bilbao": "Athletic Bilbao",
    "Athletic Club": "Athletic Bilbao",
    "Betis": "Real Betis",
    "Sociedad": "Real Sociedad",
    "Espanol": "Espanyol",
    "RCD Espanyol": "Espanyol",
    "Vallecano": "Rayo Vallecano",
    "Celta": "Celta Vigo",
    "RC Celta": "Celta Vigo",
    "Alaves": "Alaves",
    "Deportivo Alaves": "Alaves",
    "Man United": "Manchester United",
    "Man City": "Manchester City",
    "Nott'm Forest": "Nottingham Forest",
    "Wolves": "Wolverhampton Wanderers",
    "Newcastle": "Newcastle United",
    "West Ham": "West Ham United",
    "Brighton": "Brighton and Hove Albion",
    "Tottenham": "Tottenham Hotspur",
    "Leeds": "Leeds United",
    "Leicester": "Leicester City",
    "Ipswich": "Ipswich Town",
}

HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
}


def quitar_acentos(texto):
    if not texto:
        return ""
    return "".join(
        c
        for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def safe_float(row, key, default):
    val = row.get(key, "")
    try:
        return float(val) if val not in (None, "", "NA") else default
    except ValueError:
        return default


def limitar(valor, minimo, maximo):
    return max(minimo, min(maximo, valor))


def media(lista):
    return sum(lista) / len(lista) if lista else 0.0


def poisson_pmf(k, mu):
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    return (mu**k) * math.exp(-mu) / math.factorial(k)


def poisson_cdf(k, mu):
    return sum(poisson_pmf(i, mu) for i in range(int(k) + 1))


def normalizar_nombre(nombre):
    limpio = quitar_acentos(nombre).strip()
    return ALIAS_EQUIPOS.get(limpio, limpio)


def emparejar_nombre(objetivo, lista_candidatos):
    obj_norm = normalizar_nombre(objetivo)
    if obj_norm in lista_candidatos:
        return obj_norm
    coincidencias = difflib.get_close_matches(
        obj_norm, lista_candidatos, n=1, cutoff=0.45
    )
    if coincidencias:
        return coincidencias[0]
    for c in lista_candidatos:
        p1 = obj_norm.split()[0].lower()
        p2 = c.split()[0].lower()
        if (len(p1) > 3 and p1 in c.lower()) or (
            len(p2) > 3 and p2 in obj_norm.lower()
        ):
            return c
    return None


def elegir_casa_apuestas(bookmakers):
    if not bookmakers:
        return None
    casas_disponibles = {b["key"]: b for b in bookmakers}
    for clave in PRIORIDAD_CASAS:
        if clave in casas_disponibles:
            return casas_disponibles[clave]
    return bookmakers[0]


# ==========================================
# EXTRACCIÓN AUTOMÁTICA ESPN (BAJAS, XI Y MINUTOS)
# ==========================================
def extraer_stat_espn(obj, nombres_stat):
    if isinstance(obj, dict):
        nombre = str(obj.get("name", "")).lower()
        abrev = str(obj.get("abbreviation", "")).lower()
        if nombre in nombres_stat or abrev in nombres_stat:
            try:
                return float(obj.get("value", 0))
            except (ValueError, TypeError):
                pass
        for v in obj.values():
            res = extraer_stat_espn(v, nombres_stat)
            if res is not None:
                return res
    elif isinstance(obj, list):
        for item in obj:
            res = extraer_stat_espn(item, nombres_stat)
            if res is not None:
                return res
    return None


@st.cache_data(ttl=1800)  # Se renueva cada 30 min o al pulsar "Actualizar Alineaciones"
def obtener_plantilla_espn(slug_espn, team_id):
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug_espn}/teams/{team_id}/roster"
    jugadores = []
    try:
        r = requests.get(url, headers=HEADERS_WEB, timeout=12)
        if r.status_code != 200:
            return []
        for atl in r.json().get("athletes", []):
            stats_obj = atl.get("statistics", {})
            mins = extraer_stat_espn(stats_obj, {"minutes", "mins", "min"})
            apps = extraer_stat_espn(
                stats_obj, {"appearances", "starts", "gamesplayed"}
            )
            pos = atl.get("position", {}).get("abbreviation", "")
            jugadores.append(
                {
                    "id": str(atl.get("id", "")),
                    "name": atl.get("displayName", ""),
                    "pos": pos,
                    "minutos": mins if mins is not None else 0.0,
                    "apariciones": apps if apps is not None else 0.0,
                }
            )
    except Exception:
        pass
    return jugadores


@st.cache_data(ttl=900)  # Se renueva cada 15 min o al pulsar "Actualizar Alineaciones"
def obtener_contexto_espn_jornada(slug_espn):
    eventos_unicos = {}
    for d in range(7):
        fecha = (datetime.now() + timedelta(days=d)).strftime("%Y%m%d")
        try:
            r = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug_espn}/scoreboard?dates={fecha}",
                headers=HEADERS_WEB,
                timeout=10,
            )
            if r.status_code == 200:
                for ev in r.json().get("events", []):
                    eventos_unicos[ev["id"]] = ev
        except Exception:
            pass

    partidos_contexto = []
    for ev_id, ev in eventos_unicos.items():
        comp = ev.get("competitions", [{}])[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home_c = next(
            (c for c in competitors if c.get("homeAway") == "home"),
            competitors[0],
        )
        away_c = next(
            (c for c in competitors if c.get("homeAway") == "away"),
            competitors[1],
        )

        home_name = home_c.get("team", {}).get("displayName", "")
        away_name = away_c.get("team", {}).get("displayName", "")
        home_id = str(home_c.get("team", {}).get("id", ""))
        away_id = str(away_c.get("team", {}).get("id", ""))

        bajas_map = {home_name: [], away_name: []}
        xi_map = {home_name: [], away_name: []}

        try:
            r_sum = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug_espn}/summary?event={ev_id}",
                headers=HEADERS_WEB,
                timeout=10,
            )
            if r_sum.status_code == 200:
                data_sum = r_sum.json()

                # 1. Leer lesionados, sancionados y dudas
                for eq_inj in data_sum.get("injuries", []):
                    eq_n = eq_inj.get("team", {}).get("displayName", "")
                    destino = (
                        home_name
                        if emparejar_nombre(eq_n, [home_name])
                        else away_name
                    )
                    for inj in eq_inj.get("injuries", []):
                        ath = inj.get("athlete", {})
                        st_raw = str(inj.get("status", "")).lower()
                        es_duda = any(
                            k in st_raw
                            for k in [
                                "doubt",
                                "question",
                                "day-to-day",
                                "duda",
                                "probable",
                            ]
                        )
                        bajas_map[destino].append(
                            {
                                "id": str(ath.get("id", "")),
                                "name": ath.get("displayName", "Jugador"),
                                "status": inj.get("status", "Baja"),
                                "tipo": "duda" if es_duda else "baja",
                            }
                        )

                # 2. Leer alineaciones confirmadas (si ya salieron ~1h antes)
                for eq_ros in data_sum.get("rosters", []):
                    eq_n = eq_ros.get("team", {}).get("displayName", "")
                    destino = (
                        home_name
                        if emparejar_nombre(eq_n, [home_name])
                        else away_name
                    )
                    titulares = []
                    for entry in eq_ros.get("roster", []):
                        if entry.get("starter"):
                            ath = entry.get("athlete", {})
                            pos = entry.get("position", {}).get(
                                "abbreviation", ""
                            ) or ath.get("position", {}).get("abbreviation", "")
                            titulares.append(
                                {
                                    "id": str(ath.get("id", "")),
                                    "name": ath.get("displayName", ""),
                                    "pos": pos,
                                }
                            )
                    if len(titulares) >= 11:
                        xi_map[destino] = titulares
        except Exception:
            pass

        partidos_contexto.append(
            {
                "home_name": home_name,
                "away_name": away_name,
                "home_id": home_id,
                "away_id": away_id,
                "bajas": bajas_map,
                "xi": xi_map,
            }
        )
    return partidos_contexto


def evaluar_impacto_plantilla(
    slug_espn, team_id, bajas_equipo, xi_equipo, partidos_jugados_liga
):
    roster = obtener_plantilla_espn(slug_espn, team_id)
    mins_totales_liga = max(partidos_jugados_liga * 90.0, 90.0)

    # Calcular % de minutos en liga de cada jugador
    mapa_roster_id = {}
    mapa_roster_nom = {}
    porteros_roster = []

    for jug in roster:
        if jug["minutos"] > 0:
            pct = limitar((jug["minutos"] / mins_totales_liga) * 100.0, 0.0, 100.0)
        elif jug["apariciones"] > 0:
            pct = limitar(
                (jug["apariciones"] / max(partidos_jugados_liga, 1)) * 75.0,
                0.0,
                100.0,
            )
        else:
            pct = 0.0

        jug["pct_min"] = pct
        if jug["id"]:
            mapa_roster_id[jug["id"]] = jug
        if jug["name"]:
            mapa_roster_nom[quitar_acentos(jug["name"]).lower()] = jug
        if jug["pos"] in ("G", "GK"):
            porteros_roster.append(jug)

    porteros_roster.sort(key=lambda x: x["pct_min"], reverse=True)
    portero_habitual = porteros_roster[0] if porteros_roster else None

    def buscar_en_roster(jid, jname):
        if jid and jid in mapa_roster_id:
            return mapa_roster_id[jid]
        clave = quitar_acentos(jname).lower()
        if clave in mapa_roster_nom:
            return mapa_roster_nom[clave]
        return None

    xi_confirmado = len(xi_equipo) >= 11
    penalizacion_xg = 0.0
    etiquetas = []
    portero_titular_nombre = (
        portero_habitual["name"] if portero_habitual else "Habitual"
    )
    factor_portero = 1.0

    if xi_confirmado:
        ids_xi = {t["id"] for t in xi_equipo if t["id"]}
        noms_xi = {quitar_acentos(t["name"]).lower() for t in xi_equipo}

        def esta_en_xi(jid, jname):
            return (jid and jid in ids_xi) or (
                quitar_acentos(jname).lower() in noms_xi
            )

        # 1. Descontar lesionados/sancionados que siguen fuera
        ids_bajas = set()
        noms_bajas = set()
        for b in bajas_equipo:
            if b["id"]:
                ids_bajas.add(b["id"])
            noms_bajas.add(quitar_acentos(b["name"]).lower())

            if esta_en_xi(b["id"], b["name"]):
                continue  # Estaba en duda pero juega de titular

            jug_r = buscar_en_roster(b["id"], b["name"])
            pct = jug_r["pct_min"] if jug_r else 55.0
            if pct >= 30.0:
                peso_estado = 1.0 if b["tipo"] == "baja" else 0.5
                penalizacion_xg += (pct / 100.0) * 0.14 * peso_estado
                pref = "🚫 Baja" if b["tipo"] == "baja" else "⚠️ Duda fuera"
                etiquetas.append(f"{pref}: {b['name']} ({pct:.0f}% min)")

        # 2. Detectar rotaciones: jugadores sanos con >70% minutos que no son titulares
        for jug in roster:
            if (jug["id"] and jug["id"] in ids_bajas) or (
                quitar_acentos(jug["name"]).lower() in noms_bajas
            ):
                continue  # Ya contado en las bajas anteriores

            if jug["pct_min"] > 70.0 and not esta_en_xi(jug["id"], jug["name"]):
                penalizacion_xg += (jug["pct_min"] / 100.0) * 0.13
                etiquetas.append(
                    f"🔄 Rotación: {jug['name']} ({jug['pct_min']:.0f}% min)"
                )

        # 3. Comprobar qué portero sale en el XI oficial
        gk_xi = next((t for t in xi_equipo if t["pos"] in ("G", "GK")), None)
        if gk_xi:
            portero_titular_nombre = gk_xi["name"]
            jug_gk = buscar_en_roster(gk_xi["id"], gk_xi["name"])
            if (
                jug_gk
                and portero_habitual
                and jug_gk["pct_min"] < 40.0
                and portero_habitual["pct_min"] > 60.0
            ):
                factor_portero = 0.94
                portero_titular_nombre += " (Suplente)"
    else:
        # Previa antes de salir las alineaciones
        for b in bajas_equipo:
            jug_r = buscar_en_roster(b["id"], b["name"])
            pct = jug_r["pct_min"] if jug_r else 55.0
            if pct >= 30.0:
                peso_estado = 1.0 if b["tipo"] == "baja" else 0.5
                penalizacion_xg += (pct / 100.0) * 0.14 * peso_estado
                pref = "🚫 Baja" if b["tipo"] == "baja" else "⚠️ Duda (50%)"
                etiquetas.append(f"{pref}: {b['name']} ({pct:.0f}% min)")

                if (
                    jug_r
                    and jug_r["pos"] in ("G", "GK")
                    and pct >= 50.0
                    and b["tipo"] == "baja"
                ):
                    factor_portero = 0.94
                    portero_titular_nombre = f"Suplente (Baja {b['name']})"

    factor_xg = max(0.72, 1.0 - penalizacion_xg)
    return {
        "xi_confirmado": xi_confirmado,
        "factor_xg": factor_xg,
        "etiquetas": etiquetas,
        "portero_nombre": portero_titular_nombre,
        "factor_portero": factor_portero,
    }


# ==========================================
# ESTADÍSTICAS CSV, FATIGA Y CUOTAS
# ==========================================
@st.cache_data(ttl=21600)
def obtener_fatiga_calendario(fd_key):
    headers = {"X-Auth-Token": fd_key}
    desde = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    hasta = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    ultimos = {}
    for comp in ["PD", "PL", "CL"]:
        try:
            r = requests.get(
                f"https://api.football-data.org/v4/competitions/{comp}/matches?dateFrom={desde}&dateTo={hasta}",
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                for m in r.json().get("matches", []):
                    if m["status"] == "FINISHED":
                        fecha = datetime.strptime(
                            m["utcDate"][:10], "%Y-%m-%d"
                        ).date()
                        for lado in ["homeTeam", "awayTeam"]:
                            eq = m[lado]["name"]
                            if eq not in ultimos or fecha > ultimos[eq]:
                                ultimos[eq] = fecha
        except Exception:
            pass
    return ultimos


@st.cache_data(ttl=21600)
def analizar_liga_csv(url_csv):
    resp = requests.get(url_csv, headers=HEADERS_WEB, timeout=15)
    resp.raise_for_status()
    lector = csv.DictReader(io.StringIO(resp.text))

    filas = []
    goles_totales = []
    equipos_set = set()

    for row in lector:
        h_raw = row.get("HomeTeam", "").strip()
        a_raw = row.get("AwayTeam", "").strip()
        fthg = safe_float(row, "FTHG", None)
        ftag = safe_float(row, "FTAG", None)
        if not h_raw or not a_raw or fthg is None or ftag is None:
            continue

        row["HomeNorm"] = normalizar_nombre(h_raw)
        row["AwayNorm"] = normalizar_nombre(a_raw)
        filas.append(row)
        equipos_set.add(row["HomeNorm"])
        equipos_set.add(row["AwayNorm"])
        goles_totales.append((fthg + ftag) / 2.0)

    media_goles_liga = media(goles_totales) if goles_totales else 1.35
    stats = {}

    for eq in sorted(equipos_set):
        todos_eq = [
            r for r in filas if r["HomeNorm"] == eq or r["AwayNorm"] == eq
        ]
        partidos_jugados_tot = len(todos_eq)
        partidos_eq = todos_eq[-10:]
        n = len(partidos_eq)
        if n < 3:
            continue

        pesos = [0.55 + (0.45 * i / (n - 1)) for i in range(n)]

        xg_favor_list, xg_contra_list = [], []
        sot_favor_list, sot_contra_list = [], []
        tiros_tot_favor = []
        corners_favor, goles_contra_list = [], []
        xg_casa, xg_fuera = [], []
        sot_contra_casa, sot_contra_fuera = [], []

        for idx, row in enumerate(partidos_eq):
            w = pesos[idx]
            es_local = row["HomeNorm"] == eq

            gf = safe_float(row, "FTHG" if es_local else "FTAG", 1.0)
            gc = safe_float(row, "FTAG" if es_local else "FTHG", 1.0)
            sot_f = safe_float(row, "HST" if es_local else "AST", 4.0)
            sot_c = safe_float(row, "AST" if es_local else "HST", 4.0)
            sh_f = safe_float(row, "HS" if es_local else "AS", 11.0)
            sh_c = safe_float(row, "AS" if es_local else "HS", 11.0)
            cor_f = safe_float(row, "HC" if es_local else "AC", 5.0)

            factor_rival = 1.0
            avg_h = safe_float(row, "AvgH", None)
            avg_a = safe_float(row, "AvgA", None)
            if avg_h is not None and avg_a is not None:
                cuota_rival = avg_a if es_local else avg_h
                if cuota_rival < 1.80:
                    factor_rival = 1.12
                elif cuota_rival > 3.50:
                    factor_rival = 0.90

            xg_f = (
                0.32 * sot_f + 0.04 * sh_f + 0.03 * cor_f + 0.25 * gf
            ) * factor_rival
            xg_c = (0.32 * sot_c + 0.04 * sh_c + 0.25 * gc) / factor_rival

            xg_favor_list.append(xg_f * w)
            xg_contra_list.append(xg_c * w)
            sot_favor_list.append(sot_f * w)
            sot_contra_list.append(sot_c * w)
            tiros_tot_favor.append(sh_f)
            corners_favor.append(cor_f)
            goles_contra_list.append(gc)

            if es_local:
                xg_casa.append(xg_f)
                sot_contra_casa.append(sot_c)
            else:
                xg_fuera.append(xg_f)
                sot_contra_fuera.append(sot_c)

        suma_w = sum(pesos)
        xg_f_med = sum(xg_favor_list) / suma_w
        xg_c_med = sum(xg_contra_list) / suma_w
        sot_f_med = sum(sot_favor_list) / suma_w
        sot_c_med = sum(sot_contra_list) / suma_w

        total_sot_c = max(sot_c_med * n, 1.0)
        total_gc = sum(goles_contra_list)
        pct_paradas = limitar(1.0 - (total_gc / total_sot_c), 0.50, 0.90)

        precision_tiro = media(sot_favor_list) / max(media(tiros_tot_favor), 1.0)
        pct_fuera_area_est = limitar((1.0 - precision_tiro) * 65.0, 22.0, 55.0)

        stats[eq] = {
            "partidos_jugados": partidos_jugados_tot,
            "att_home": (
                0.65 * xg_f_med
                + 0.35 * (media(xg_casa) if xg_casa else xg_f_med)
            )
            / media_goles_liga,
            "att_away": (
                0.65 * xg_f_med
                + 0.35 * (media(xg_fuera) if xg_fuera else xg_f_med)
            )
            / media_goles_liga,
            "def_ratio": xg_c_med / media_goles_liga,
            "corners_med": media(corners_favor),
            "sot_favor": sot_f_med,
            "sot_contra_home": (
                media(sot_contra_casa) if sot_contra_casa else sot_c_med
            ),
            "sot_contra_away": (
                media(sot_contra_fuera) if sot_contra_fuera else sot_c_med
            ),
            "pct_paradas": pct_paradas,
            "pct_fuera_area": pct_fuera_area_est,
            "media_liga": media_goles_liga,
        }
    return stats


@st.cache_data(ttl=21600)
def obtener_cuotas_liga(slug_odds, api_key):
    r = requests.get(
        f"https://api.the-odds-api.com/v4/sports/{slug_odds}/odds",
        params={
            "apiKey": api_key,
            "bookmakers": "winamax_fr,winamax_de,bet365,pinnacle,unibet_eu",
            "markets": "spreads",
            "oddsFormat": "decimal",
        },
        timeout=15,
    )
    if r.status_code == 200:
        return r.json(), r.headers.get("x-requests-remaining", "?")
    return [], "Error"


def prob_handicap_asiatico(xg_l, xg_v, linea, es_local):
    max_g = 9
    p_l = [poisson_pmf(k, xg_l) for k in range(max_g)]
    p_v = [poisson_pmf(k, xg_v) for k in range(max_g)]

    sublineas = (
        [linea - 0.25, linea + 0.25]
        if round((linea * 4) % 2) == 1
        else [linea]
    )

    p_efectiva = 0.0
    for sl in sublineas:
        w, l = 0.0, 0.0
        for gl in range(max_g):
            for gv in range(max_g):
                prob_marcador = p_l[gl] * p_v[gv]
                res = ((gl - gv) if es_local else (gv - gl)) + sl
                if res > 0:
                    w += prob_marcador
                elif res < 0:
                    l += prob_marcador
        p_efectiva += w / max(w + l, 1e-6)
    return p_efectiva / len(sublineas)


# ==========================================
# INTERFAZ VISUAL DE LA APLICACIÓN (V2)
# ==========================================
st.title("⚽ Radar Fútbol EV+")
st.caption("Bajas y Rotaciones Automáticas (>70% min) | Filtro ≥ +2,5%")

liga_sel = st.selectbox(
    "Competición",
    ["LaLiga (España)", "Premier League (Inglaterra)"],
)

c_btn1, c_btn2 = st.columns(2)
with c_btn1:
    if st.button("🔄 Actualizar Alineaciones", use_container_width=True):
        obtener_contexto_espn_jornada.clear()
        obtener_plantilla_espn.clear()
        st.rerun()
with c_btn2:
    if st.button("💰 Actualizar Todo (Cuotas)", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if "LaLiga" in liga_sel:
    url_csv = "https://www.football-data.co.uk/mmz4281/2627/SP1.csv"
    slug_odds = "soccer_spain_la_liga"
    slug_espn = "esp.1"
else:
    url_csv = "https://www.football-data.co.uk/mmz4281/2627/E0.csv"
    slug_odds = "soccer_epl"
    slug_espn = "eng.1"

ultimos_partidos = obtener_fatiga_calendario(FOOTBALL_DATA_KEY)
stats = analizar_liga_csv(url_csv)
eventos, creditos = obtener_cuotas_liga(slug_odds, ODDS_API_KEY)
contextos_espn = obtener_contexto_espn_jornada(slug_espn)

st.caption(
    f"Créditos API restantes: **{creditos}** (Pulsar 'Actualizar Alineaciones' gasta **0 créditos**)"
)

tab_ha, tab_porteros = st.tabs(
    ["📊 Hándicap Asiático", "🧤 Paradas de Porteros"]
)

# ==========================================
# PREPARAR DATOS CRUZADOS POR PARTIDO
# ==========================================
nombres_csv = list(stats.keys())
partidos_procesados = []

for ev in eventos:
    loc = emparejar_nombre(ev["home_team"], nombres_csv)
    vis = emparejar_nombre(ev["away_team"], nombres_csv)
    if not loc or not vis:
        continue

    sl, sv = stats[loc], stats[vis]

    # Buscar partido equivalente en ESPN para bajas, minutos y alineaciones
    ctx_match = None
    for c_espn in contextos_espn:
        if emparejar_nombre(c_espn["home_name"], [loc]) and emparejar_nombre(
            c_espn["away_name"], [vis]
        ):
            ctx_match = c_espn
            break

    if ctx_match:
        h_n = ctx_match["home_name"]
        a_n = ctx_match["away_name"]
        eval_loc = evaluar_impacto_plantilla(
            slug_espn,
            ctx_match["home_id"],
            ctx_match["bajas"].get(h_n, []),
            ctx_match["xi"].get(h_n, []),
            sl["partidos_jugados"],
        )
        eval_vis = evaluar_impacto_plantilla(
            slug_espn,
            ctx_match["away_id"],
            ctx_match["bajas"].get(a_n, []),
            ctx_match["xi"].get(a_n, []),
            sv["partidos_jugados"],
        )
    else:
        eval_loc = {
            "xi_confirmado": False,
            "factor_xg": 1.0,
            "etiquetas": [],
            "portero_nombre": "Habitual",
            "factor_portero": 1.0,
        }
        eval_vis = {
            "xi_confirmado": False,
            "factor_xg": 1.0,
            "etiquetas": [],
            "portero_nombre": "Habitual",
            "factor_portero": 1.0,
        }

    # Cálculo xG base + balón parado + bajas/rotaciones automáticas
    xg_l = (
        sl["att_home"]
        * sv["def_ratio"]
        * sl["media_liga"]
        * 1.07
        * eval_loc["factor_xg"]
    )
    xg_v = (
        sv["att_away"]
        * sl["def_ratio"]
        * sv["media_liga"]
        * 0.93
        * eval_vis["factor_xg"]
    )

    if sl["corners_med"] > 6.5:
        xg_l *= 1.03
    if sv["corners_med"] > 6.5:
        xg_v *= 1.03

    fecha_p = datetime.strptime(ev["commence_time"][:10], "%Y-%m-%d").date()
    fatiga_notas = []
    for eq_fd, f_ult in ultimos_partidos.items():
        if emparejar_nombre(loc, [eq_fd]) and (fecha_p - f_ult).days < 4:
            xg_l *= 0.93
            fatiga_notas.append(f"Fatiga {loc}")
        if emparejar_nombre(vis, [eq_fd]) and (fecha_p - f_ult).days < 4:
            xg_v *= 0.93
            fatiga_notas.append(f"Fatiga {vis}")

    partidos_procesados.append(
        {
            "ev": ev,
            "loc": loc,
            "vis": vis,
            "sl": sl,
            "sv": sv,
            "xg_l": xg_l,
            "xg_v": xg_v,
            "fecha_p": fecha_p,
            "fatiga_notas": fatiga_notas,
            "eval_loc": eval_loc,
            "eval_vis": eval_vis,
        }
    )

# ==========================================
# PESTAÑA 1: HÁNDICAP ASIÁTICO AUTOMÁTICO
# ==========================================
with tab_ha:
    solo_verdes = st.toggle("Mostrar solo 🟢 Alertas Verdes (+2,5%)", value=False)
    hay_resultados = False

    for p in partidos_procesados:
        ev = p["ev"]
        casa = elegir_casa_apuestas(ev.get("bookmakers"))
        if not casa:
            continue

        opciones_validas = []
        for m in casa.get("markets", []):
            if m["key"] == "spreads":
                for out in m["outcomes"]:
                    cuota = out["price"]
                    if 1.50 <= cuota <= 2.00:
                        es_loc = out["name"] == ev["home_team"]
                        linea = float(out.get("point", 0))
                        p_mod = (
                            prob_handicap_asiatico(
                                p["xg_l"], p["xg_v"], linea, es_loc
                            )
                            * 100
                        )
                        p_imp = (1 / cuota) * 100
                        ventaja = p_mod - p_imp
                        es_verde = ventaja >= 2.5
                        if solo_verdes and not es_verde:
                            continue
                        opciones_validas.append(
                            (
                                out["name"],
                                linea,
                                cuota,
                                p_mod,
                                p_imp,
                                ventaja,
                                es_verde,
                            )
                        )

        if not opciones_validas:
            continue

        hay_resultados = True
        xi_ok = p["eval_loc"]["xi_confirmado"] and p["eval_vis"]["xi_confirmado"]
        estado_xi = "✅ XI Oficial" if xi_ok else "⏳ Previa (sin XI)"

        with st.container(border=True):
            st.subheader(f"{p['loc']} vs {p['vis']}")
            info_extra = f"📅 {p['fecha_p']} | {estado_xi} | xG Est: **{p['xg_l']:.2f} - {p['xg_v']:.2f}**"
            if p["fatiga_notas"]:
                info_extra += f" | ⚡ {', '.join(p['fatiga_notas'])}"
            st.caption(info_extra)

            # Mostrar bajas y rotaciones detectadas automáticamente
            todas_etiquetas = [
                f"**{p['loc']}**: {e}" for e in p["eval_loc"]["etiquetas"]
            ] + [f"**{p['vis']}**: {e}" for e in p["eval_vis"]["etiquetas"]]

            if todas_etiquetas:
                st.markdown("  \n".join(todas_etiquetas))
            else:
                st.caption("Plantillas sin bajas clave ni rotaciones (>70% min) detectadas.")

            for nombre_ap, linea, cuota, p_mod, p_imp, ventaja, es_verde in opciones_validas:
                texto = (
                    f"**{nombre_ap} ({linea:+.2f})** @ **{cuota:.2f}** ({casa['title']})  \n"
                    f"Tu Prob: **{p_mod:.1f}%** vs Casa: {p_imp:.1f}% → Ventaja: **{ventaja:+.2f}%**"
                )
                if es_verde:
                    st.success(f"🟢 ALERTA VERDE  \n{texto}")
                else:
                    st.info(f"⚪ Sin ventaja suficiente  \n{texto}")

    if not hay_resultados:
        st.warning("No hay partidos que cumplan el filtro seleccionado ahora mismo.")

# ==========================================
# PESTAÑA 2: PARADAS DE PORTEROS (SOLO PROYECCIÓN)
# ==========================================
with tab_porteros:
    st.caption(
        "Proyección automática de paradas y cuota mínima rentable (+2,5% valor) para comparar de un vistazo en Winamax o Bet365."
    )

    for p in partidos_procesados:
        sl, sv = p["sl"], p["sv"]
        pct_par_loc = sl["pct_paradas"] * p["eval_loc"]["factor_portero"]
        pct_par_vis = sv["pct_paradas"] * p["eval_vis"]["factor_portero"]

        sot_loc = sl["sot_contra_home"] * 0.55 + sv["sot_favor"] * 0.45
        paradas_loc = sot_loc * pct_par_loc

        sot_vis = sv["sot_contra_away"] * 0.55 + sl["sot_favor"] * 0.45
        paradas_vis = sot_vis * pct_par_vis

        xi_ok = p["eval_loc"]["xi_confirmado"] and p["eval_vis"]["xi_confirmado"]
        estado_xi = "✅ XI Oficial" if xi_ok else "⏳ Previa"

        with st.container(border=True):
            st.subheader(f"{p['loc']} vs {p['vis']}")
            st.caption(f"📅 {p['fecha_p']} | {estado_xi}")

            col_l, col_v = st.columns(2)
            with col_l:
                st.markdown(f"**{p['loc']}** ({p['eval_loc']['portero_nombre']})")
                st.metric("Paradas esperadas", f"{paradas_loc:.2f}")
                st.caption(
                    f"Recibe: {sot_loc:.1f} tiros/p | % Paradas: {pct_par_loc*100:.0f}%  \n"
                    f"Tiros lejanos rival: {sv['pct_fuera_area']:.0f}%"
                )
                for lin in [1.5, 2.5, 3.5, 4.5]:
                    pr = 1.0 - poisson_cdf(lin, paradas_loc)
                    if pr > 0.05:
                        cuota_min = 1.0 / max(pr - 0.025, 0.05)
                        marca = "🎯" if 1.50 <= cuota_min <= 2.00 else "•"
                        st.caption(
                            f"{marca} **+{lin}**: {pr*100:.0f}% (Cuota ≥ **{cuota_min:.2f}**)"
                        )

            with col_v:
                st.markdown(f"**{p['vis']}** ({p['eval_vis']['portero_nombre']})")
                st.metric("Paradas esperadas", f"{paradas_vis:.2f}")
                st.caption(
                    f"Recibe: {sot_vis:.1f} tiros/p | % Paradas: {pct_par_vis*100:.0f}%  \n"
                    f"Tiros lejanos rival: {sl['pct_fuera_area']:.0f}%"
                )
                for lin in [1.5, 2.5, 3.5, 4.5]:
                    pr = 1.0 - poisson_cdf(lin, paradas_vis)
                    if pr > 0.05:
                        cuota_min = 1.0 / max(pr - 0.025, 0.05)
                        marca = "🎯" if 1.50 <= cuota_min <= 2.00 else "•"
                        st.caption(
                            f"{marca} **+{lin}**: {pr*100:.0f}% (Cuota ≥ **{cuota_min:.2f}**)"
                        )
