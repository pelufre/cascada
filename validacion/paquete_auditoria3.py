"""Arma el ZIP de respuesta a la segunda auditoría: repositorio desde el commit que auditó (git bundle incremental
c08d856..HEAD, se aplica sobre el clon de cascada.bundle del primer paquete), salidas de pruebas y corridas con el motor
1.3, el script del auditor corrido contra el código nuevo y la respuesta punto por punto (AUDITORIA3.md). Los datos no
cambiaron (mismas huellas del primer paquete) y no se vuelven a mandar.

    python -m validacion.paquete_auditoria3 --salida Cascada_1.3_respuesta_auditoria2.zip [--extra archivo …]
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
AUDITADO = "c08d856ec64a356e8103cab3675a972fb9f979c9"
PRUEBAS = ["tests.test_auditoria", "tests.test_servicio", "tests.test_config", "tests.test_subcuenta", "tests.test_auditoria2"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main(a):
    tmp = Path(tempfile.mkdtemp())
    raiz = tmp / "Cascada_1.3_respuesta_auditoria2"
    (raiz / "salidas").mkdir(parents=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=RAIZ, capture_output=True, text=True, check=True).stdout.strip()
    rama = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=RAIZ, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "bundle", "create", str(raiz / "cascada_desde_c08d856.bundle"), f"{AUDITADO}..{rama}"],
                   cwd=RAIZ, check=True)
    with open(raiz / "salidas" / "pruebas.txt", "w") as f:
        for m in PRUEBAS:
            r = subprocess.run([sys.executable, "-m", m], cwd=RAIZ, capture_output=True, text=True)
            f.write(f"$ python -m {m}\n{r.stdout}\n")
    res = RAIZ / "validacion" / "resultados"
    for sub, patron in (("revalidacion", "*.json"), ("auditoria3", "*.json")):
        for p in sorted((res / sub).glob(patron)):
            shutil.copy(p, raiz / "salidas" / f"{sub}_{p.name}")
    for p in (RAIZ / "validacion" / "PROTOCOLO.md", RAIZ / "validacion" / "REGISTRO.md"):
        shutil.copy(p, raiz / "salidas" / p.name)
    for extra in a.extra or []:
        shutil.copy(extra, raiz / "salidas" / Path(extra).name)
    shutil.copy(RAIZ / "validacion" / "AUDITORIA3.md", raiz / "LEEME.md")
    with open(raiz / "SHA256SUMS.txt", "w") as f:
        f.write(f"# commit HEAD del bundle: {head} (rama {rama}); se aplica sobre {AUDITADO}\n")
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
    ap.add_argument("--salida", required=True)
    ap.add_argument("--extra", nargs="*", help="otras salidas a incluir")
    main(ap.parse_args())
