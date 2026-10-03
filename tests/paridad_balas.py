"""Paridad de 30 balas: motor en vivo (paso a paso) contra el backtest (cartera_balas.run, lev 5).
Uso: python -m tests.paridad_balas --backtest /ruta/a/carpeta_con_balas_py"""
import argparse, sys, time
from pathlib import Path
import numpy as np, pandas as pd
from cascada.balas import Balas
from cascada.db import Base


def main(a):
    sys.path.insert(0, a.backtest)
    import balas as bt, aberration_engine as ae
    eq, _, _, info = bt.run(lev=5.0)
    b = ae.bars("BTC", 4)
    fr = bt.funding_4h(b.index)
    ruta = Path(a.base); ruta.unlink(missing_ok=True)
    db = Base(ruta)
    m = Balas(db, capital_papel=1.0)
    C = b.close
    vivo = np.empty(len(b)); t0 = time.time()
    for i in range(len(b)):
        t = b.index[i] + pd.Timedelta(hours=4)
        m.procesar(t, (b.open.iat[i], b.high.iat[i], b.low.iat[i], b.close.iat[i]),
                   C.iloc[max(0, i - 300):i + 1], eventos_funding=[(t, fr[i])])
        vivo[i] = m.patrimonio(b.close.iat[i])
    v = pd.Series(vivo, index=b.index)
    # el backtest valoriza la barra i con lo ejecutado en su apertura; el vivo ejecuta al cierre de la anterior:
    # comparamos el patrimonio al cierre, que debe coincidir salvo el desfase de una barra en las ejecuciones
    r = pd.DataFrame(dict(bt=eq, vivo=v))
    print(f"{time.time()-t0:.0f} s | campañas bt {info['camps']} vivo {m.st['camps']} | liq bt {info['liqs']} vivo {m.st['liqs']}")
    print("final", r.iloc[-1].round(4).to_dict())
    print("diferencia relativa máx", float((r.vivo / r.bt - 1).abs().max()))
    d = r.resample("D").last().pct_change().dropna()
    print("corr diaria", round(d.corr().iat[0, 1], 4))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--backtest", default="/home/claude/w4"); ap.add_argument("--base", default="/tmp/paridad_balas.db")
    main(ap.parse_args())
