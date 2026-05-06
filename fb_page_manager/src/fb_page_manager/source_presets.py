"""Built-in source presets for Mexico-focused celebrity storytelling."""

from __future__ import annotations

from typing import Dict, List

YOUTUBE_NEWS_CHANNELS: List[str] = [
    "https://www.youtube.com/@JoseGonzalezTVV/videos",
    "https://www.youtube.com/@EmmaHavokOscuro/videos",
    "https://www.youtube.com/@sussexdailynewsver.2143/videos",
    "https://www.youtube.com/@chuntillo/videos",
]

YOUTUBE_BRUCE_LEE_CHANNELS: List[str] = [
    "https://www.youtube.com/@BruceLeeEncounters/videos",
    "https://www.youtube.com/@CRECEmotivacion/videos",
    "https://www.youtube.com/@BruceLeeElLegadoOculto-2b/videos",
    "https://www.youtube.com/@BruceLeeLoNuncaVisto/videos",
    "https://www.youtube.com/@BeerdyBruceLeeCentral/videos",
]

YOUTUBE_CELEB_CHANNELS: List[str] = [
    "https://www.youtube.com/@ElReydelTiempo1970s",
    "https://www.youtube.com/@HistoriasdePedroInfante/videos",
    "https://www.youtube.com/@el_historiador___/videos",
    "https://www.youtube.com/@EstrellasMalditas/videos",
    "https://www.youtube.com/@RelatosdeCantinflasyt",
    "https://www.youtube.com/@tutorialesgerberin/videos",
    "https://www.youtube.com/@SusurrosdeHollywood/videos",
    "https://www.youtube.com/@MitosFamosos/videos",
    "https://www.youtube.com/@N%C3%BAcleoInformativo1",
    "https://www.youtube.com/@soymachito/videos",
    "https://www.youtube.com/@SecretosfamososYT/videos",
    "https://www.youtube.com/@RELATOSPEDROINFANTE",
    "https://www.youtube.com/@TestigosdelCartel/videos",
    "https://www.youtube.com/@SECRETOSDESANGRE58/videos",
    "https://www.youtube.com/@sombrasdelafamayta",
    "https://www.youtube.com/@Hijos_del_Poder",
    "https://www.youtube.com/@HijosdelaFama1",
    "https://www.youtube.com/@JuanGabrielElLegadoOculto",
    "https://www.youtube.com/@Hombres_con_Poder",
    "https://www.youtube.com/@cuandoeldestinohabla/videos",
    "https://www.youtube.com/@VerikBenBlesson/videos",
    "https://www.youtube.com/@ZonaCelebrity",
    "https://www.youtube.com/@Mexicoescandaloso",
    "https://www.youtube.com/@SusurrosdeHollywood/videos",
    "https://www.youtube.com/@Cr%C3%B3nicasdelChapo/videos",
    "https://www.youtube.com/@Historiasdeoroyta",
    "https://www.youtube.com/@HiNoorSp",
    "https://www.youtube.com/@SECRETOSDESANGRE58",
    "https://www.youtube.com/@DieckDocks/videos",
    "https://www.youtube.com/@MiBellaMar%C3%ADaF%C3%A9lix",
]

NEWS_ARTICLES: List[str] = [
    "https://www.hola.com/us/celebrities/20250407824884/cazzu-feelings-towards-nodal-angela-aguilar/#google_vignette",
    "https://www.rollingstone.com/music/music-latin/christan-nodal-angela-aguilar-cazzu-relationship-timeline-1235182401/",
    "https://www.elespectador.com/revista-vea/lo-ultimo/cazzu-desmiente-a-angela-aguilar-y-habla-sin-tapujos-he-soportado-en-silencio/",
    "https://www.infobae.com/mexico/2025/04/05/cazzu-asegura-que-no-juzga-a-angela-aguilar-y-desmiente-problemas-legales-con-christian-nodal/",
    "https://www.univision.com/estilo-de-vida/cazzu-hablo-de-nodal-y-angela-pero-eso-no-la-hace-mejor-que-otras-famosas-con-el-corazon-roto-video",
    "https://www.aztecayucatan.com/espectaculos/esta-fue-respuesta-de-cazzu-polemicas-declaraciones-angela-aguilar-sobre-su-matrimonio-christian-nodal",
    "https://www.pulzo.com/entretenimiento/cuantos-anos-tiene-angela-aguilar-vs-cazzu-christian-nodal-PP3708333",
]


def default_youtube_channels() -> List[str]:
    merged = YOUTUBE_NEWS_CHANNELS + YOUTUBE_BRUCE_LEE_CHANNELS + YOUTUBE_CELEB_CHANNELS
    seen = set()
    deduped: List[str] = []
    for url in merged:
        cleaned = url.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def get_source_groups() -> Dict[str, List[str]]:
    return {
        "youtube_news": list(YOUTUBE_NEWS_CHANNELS),
        "youtube_bruce_lee": list(YOUTUBE_BRUCE_LEE_CHANNELS),
        "youtube_celebrity": list(YOUTUBE_CELEB_CHANNELS),
        "news_articles": list(NEWS_ARTICLES),
    }
