"""Arma el ZIP para la segunda auditoría: repositorio con toda su historia (git bundle), datos con huellas SHA-256,
salidas de las pruebas y de las corridas, y la respuesta punto por punto (validacion/AUDITORIA2.md).

    python -m validacion.paquete_auditoria --salida Cascada_1.2_auditoria.zip --perp_zip … --spot … \
        --top50 … --resumen … --universo … --xbt … --riesgo …
"""
import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PRUEBAS = ["tests.test_auditoria", "tests.test_servicio", "tests.test_config", "tests.test_subcuenta"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(a):
    tmp = Path(tempfile.mkdtemp())
    raiz = tmp / "Cascada_1.2_auditoria"
    (raiz / "datos" / "top50").mkdir(parents=True)
    (raiz / "datos" / "spot_4h").mkdir()
    (raiz / "salidas").mkdir()
    # 1) repositorio con toda la historia
    subprocess.run(["git", "bundle", "create", str(raiz / "cascada.bundle"), "--all"], cwd=RAIZ, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=RAIZ, capture_output=True, text=True).stdout.strip()
    # 2) datos
    shutil.copy(a.perp_zip, raiz / "datos" / "perpetuos_4h.zip")
    for f in Path(a.spot).glob("_*_4h.csv"):
        shutil.copy(f, raiz / "datos" / "spot_4h" / f.name)
    for origen, nombre in ((a.top50, "top50_semanal_2.csv"), (a.resumen, "resumen_descarga_1.csv"), (a.universo, "universo_1.csv")):
        shutil.copy(origen, raiz / "datos" / "top50" / nombre)
    shutil.copy(a.xbt, raiz / "datos" / "funding_XBTUSDM.csv")
    shutil.copy(a.riesgo, raiz / "datos" / "niveles_riesgo_XBTUSDM.csv")
    # 3) salidas: pruebas y corridas
    with open(raiz / "salidas" / "pruebas.txt", "w") as f:
        for m in PRUEBAS:
            r = subprocess.run([sys.executable, "-m", m], cwd=RAIZ, capture_output=True, text=True)
            f.write(f"$ python -m {m}\n{r.stdout}\n")
    res = RAIZ / "validacion" / "resultados"
    for n in ["seleccion_is.json", "is_resumen.json", "resultado_oos.json", "resultado_oos_nivel10.json",
              "resultado_oos_nivel20.json", "resultado_oos_nivel25.json", "verificacion_10.json", "verificacion_20.json",
              "verificacion_25.json", "verificacion_30.json", "auditoria2/resumen.json"]:
        if (res / n).exists():
            shutil.copy(res / n, raiz / "salidas" / n.replace("/", "_"))
    shutil.copy(RAIZ / "validacion" / "pesos_congelados.json", raiz / "salidas" / "pesos_congelados.json")
    for extra in a.extra or []:
        shutil.copy(extra, raiz / "salidas" / Path(extra).name)
    # 4) respuesta y huellas
    shutil.copy(RAIZ / "validacion" / "AUDITORIA2.md", raiz / "LEEME.md")
    with open(raiz / "SHA256SUMS.txt", "w") as f:
        f.write(f"# commit HEAD del bundle: {head}\n")
        for p in sorted(raiz.rglob("*")):
            if p.is_file() and p.name != "SHA256SUMS.txt":
                f.write(f"{sha(p)}  {p.relative_to(raiz)}\n")
    salida = Path(a.salida)
    with zipfile.ZipFile(salida, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(raiz.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(tmp))
    print(salida, f"{salida.stat().st_size / 1e6:.1f} MB · HEAD {head[:7]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    for k in ("salida", "perp_zip", "spot", "top50", "resumen", "universo", "xbt", "riesgo"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--extra", nargs="*", help="otras salidas a incluir (simulación del servicio, etc.)")
    main(ap.parse_args())
