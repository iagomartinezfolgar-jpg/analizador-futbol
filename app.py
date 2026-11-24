import csv
from datetime import datetime, timedelta
import difflib
import io
import math
import requests
import streamlit as st

# ==========================================
# CONFIGURACIÓN DE PÁGINA (DISEÑO MÓVIL)
# ==========================================
st.set_page_config(
    page_title="Radar Fútbol EV+",
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
    "Betis": "Real Betis",
    "Sociedad": "Real Sociedad",
    "Espanol": "Espanyol",
    "Vallecano": "Rayo Vallecano",
    "Celta": "Celta Vigo",
    "Alaves": "Alaves",
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
    return ALIAS_EQUIPOS.get(nombre, nombre)


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
        if (
            obj_norm.split()[0].lower() in c.lower()
            or c.split()[0].lower() in obj_norm.lower()
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
# FUNCIONES CON CACHÉ (AHORRO DE CRÉDITOS)
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
    resp = requests.get(
        url_csv, headers={"User-Agent": "Mozilla/5.0"}, timeout=15
    )
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
        partidos_eq = [
            r for r in filas if r["HomeNorm"] == eq or r["AwayNorm"] == eq
        ][-10:]
        n = len(partidos_eq)
        if n < 3:
            continue

        # Pesos decrecientes: el más reciente vale 1.00 y el 10º anterior vale 0.55
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
# INTERFAZ VISUAL DE LA APLICACIÓN
# ==========================================
st.title("⚽ Radar Fútbol EV+")
st.caption("Filtro automático: Ventaja ≥ 2,5% | Cuotas 1,50 – 2,00")

col_liga, col_btn = st.columns([2, 1])
with col_liga:
    liga_sel = st.selectbox(
        "Competición",
        ["LaLiga (España)", "Premier League (Inglaterra)"],
    )
with col_btn:
    st.write("")
    st.write("")
    if st.button("🔄 Actualizar"):
        st.cache_data.clear()
        st.rerun()

if "LaLiga" in liga_sel:
    url_csv = "https://www.football-data.co.uk/mmz4281/2627/SP1.csv"
    slug_odds = "soccer_spain_la_liga"
else:
    url_csv = "https://www.football-data.co.uk/mmz4281/2627/E0.csv"
    slug_odds = "soccer_epl"

ultimos_partidos = obtener_fatiga_calendario(FOOTBALL_DATA_KEY)
stats = analizar_liga_csv(url_csv)
eventos, creditos = obtener_cuotas_liga(slug_odds, ODDS_API_KEY)

st.caption(f"Créditos API restantes este mes: **{creditos}** (Caché 6h activo)")

tab_ha, tab_porteros = st.tabs(
    ["📊 Hándicap Asiático", "🧤 Paradas de Porteros"]
)

# ==========================================
# PESTAÑA 1: HÁNDICAP ASIÁTICO
# ==========================================
with tab_ha:
    solo_verdes = st.toggle("Mostrar solo 🟢 Alertas Verdes (+2,5%)", value=False)
    nombres_csv = list(stats.keys())
    hay_resultados = False

    for ev in eventos:
        loc = emparejar_nombre(ev["home_team"], nombres_csv)
        vis = emparejar_nombre(ev["away_team"], nombres_csv)
        if not loc or not vis:
            continue

        sl, sv = stats[loc], stats[vis]
        xg_l = sl["att_home"] * sv["def_ratio"] * sl["media_liga"] * 1.07
        xg_v = sv["att_away"] * sl["def_ratio"] * sl["media_liga"] * 0.93

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
                            prob_handicap_asiatico(xg_l, xg_v, linea, es_loc)
                            * 100
                        )
                        p_imp = (1 / cuota) * 100
                        ventaja = p_mod - p_imp
                        es_verde = ventaja >= 2.5
                        if solo_verdes and not es_verde:
                            continue
                        opciones_validas.append(
                            (out["name"], linea, cuota, p_mod, p_imp, ventaja, es_verde)
                        )

        if not opciones_validas:
            continue

        hay_resultados = True
        with st.container(border=True):
            st.subheader(f"{loc} vs {vis}")
            info_extra = f"📅 {fecha_p} | xG Est: **{xg_l:.2f} - {xg_v:.2f}**"
            if fatiga_notas:
                info_extra += f" | ⚠️ {', '.join(fatiga_notas)}"
            st.caption(info_extra)

            c1, c2 = st.columns(2)
            baja_loc = c1.checkbox(f"Baja clave {loc}", key=f"bl_{ev['id']}")
            baja_vis = c2.checkbox(f"Baja clave {vis}", key=f"bv_{ev['id']}")

            xg_l_adj = xg_l * (0.88 if baja_loc else 1.0)
            xg_v_adj = xg_v * (0.88 if baja_vis else 1.0)

            for nombre_ap, linea, cuota, _, p_imp, _, _ in opciones_validas:
                es_loc = nombre_ap == ev["home_team"]
                p_mod = (
                    prob_handicap_asiatico(xg_l_adj, xg_v_adj, linea, es_loc)
                    * 100
                )
                ventaja = p_mod - p_imp
                texto = (
                    f"**{nombre_ap} ({linea:+.2f})** @ **{cuota:.2f}** ({casa['title']})  \n"
                    f"Tu Prob: **{p_mod:.1f}%** vs Casa: {p_imp:.1f}% → Ventaja: **{ventaja:+.2f}%**"
                )
                if ventaja >= 2.5:
                    st.success(f"🟢 ALERTA VERDE  \n{texto}")
                else:
                    st.info(f"⚪ Sin ventaja suficiente  \n{texto}")

    if not hay_resultados:
        st.warning("No hay partidos que cumplan el filtro seleccionado ahora mismo.")

# ==========================================
# PESTAÑA 2: PARADAS DE PORTEROS
# ==========================================
with tab_porteros:
    st.caption(
        "Introduce la línea y cuota de Winamax o Bet365 para comprobar si dispara Alerta Verde."
    )
    nombres_csv = list(stats.keys())

    for ev in eventos:
        loc = emparejar_nombre(ev["home_team"], nombres_csv)
        vis = emparejar_nombre(ev["away_team"], nombres_csv)
        if not loc or not vis:
            continue

        sl, sv = stats[loc], stats[vis]
        sot_loc = sl["sot_contra_home"] * 0.55 + sv["sot_favor"] * 0.45
        paradas_loc = sot_loc * sl["pct_paradas"]

        sot_vis = sv["sot_contra_away"] * 0.55 + sl["sot_favor"] * 0.45
        paradas_vis = sot_vis * sv["pct_paradas"]

        with st.container(border=True):
            st.subheader(f"{loc} vs {vis}")

            col_l, col_v = st.columns(2)
            with col_l:
                st.markdown(f"**Portero {loc}**")
                st.metric("Paradas esperadas", f"{paradas_loc:.2f}")
                st.caption(
                    f"Recibe: {sot_loc:.1f} tiros/p  \n"
                    f"% Paradas: {sl['pct_paradas']*100:.0f}%  \n"
                    f"Tiros lejanos rival: {sv['pct_fuera_area']:.0f}%"
                )
            with col_v:
                st.markdown(f"**Portero {vis}**")
                st.metric("Paradas esperadas", f"{paradas_vis:.2f}")
                st.caption(
                    f"Recibe: {sot_vis:.1f} tiros/p  \n"
                    f"% Paradas: {sv['pct_paradas']*100:.0f}%  \n"
                    f"Tiros lejanos rival: {sl['pct_fuera_area']:.0f}%"
                )

            with st.expander("🧮 Calcular cuota de Winamax / Bet365"):
                c_eq, c_lin, c_cuo = st.columns([2, 1, 1])
                portero_sel = c_eq.selectbox(
                    "Portero", [loc, vis], key=f"p_eq_{ev['id']}"
                )
                linea_p = c_lin.selectbox(
                    "Más de",
                    [1.5, 2.5, 3.5, 4.5, 5.5],
                    index=1,
                    key=f"p_lin_{ev['id']}",
                )
                cuota_p = c_cuo.number_input(
                    "Cuota",
                    min_value=1.01,
                    max_value=10.0,
                    value=1.75,
                    step=0.05,
                    key=f"p_cuo_{ev['id']}",
                )

                esp_sel = paradas_loc if portero_sel == loc else paradas_vis
                prob_over = (1 - poisson_cdf(linea_p, esp_sel)) * 100
                prob_imp_p = (1 / cuota_p) * 100
                ventaja_p = prob_over - prob_imp_p

                res_txt = (
                    f"Más de {linea_p} paradas ({portero_sel}) @ {cuota_p:.2f}  \n"
                    f"Tu Prob: **{prob_over:.1f}%** vs Casa: {prob_imp_p:.1f}% → Ventaja: **{ventaja_p:+.2f}%**"
                )
                if ventaja_p >= 2.5 and 1.50 <= cuota_p <= 2.00:
                    st.success(f"🟢 ALERTA VERDE  \n{res_txt}")
                else:
                    st.info(f"⚪ No cumple filtro (≥ +2,5% y cuota 1,50–2,00)  \n{res_txt}")
