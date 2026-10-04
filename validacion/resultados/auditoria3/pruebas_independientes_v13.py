# Script de pruebas independientes de la segunda auditoría (pruebas_independientes.py del auditor), con SOLO las
# llamadas cuya firma cambió adaptadas al código 1.3 (procesar sin funding_8h; falsos con saldos). La evaluación de sus
# 9 detecciones contra el código nuevo está en pruebas_independientes_v13.json (ver AUDITORIA3.md §1).
import sys,json,logging,os
from pathlib import Path
import pandas as pd
repo=Path(os.environ.get('CASCADA_REPO','/workspace/scratch/ea6c28c152f8/revision_v12/cascada'));sys.path.insert(0,str(repo))
logging.disable(logging.CRITICAL)
from tests.test_auditoria import montar,lote,ciclo,T0,H4,libro_neto
from cascada.balas import Balas
from cascada.db import Base
from cascada.motor import Motor
from cascada.bolsa import KucoinReal,Publico
from tests.test_auditoria import FakeDatos,FakeEst
out={}
# Dos señales contrarias sobre un símbolo inicialmente plano.
m,b,e,db=montar(); e.deseos=[lote('largo',frac=.1,lado=1),lote('corto',frac=.05,lado=-1)]
ciclo(m,T0)
out['opuestos_desde_plano']={'exchange':b.posiciones(),'libro':libro_neto(m),'conciliado':db.get('conciliacion_ok')}
# Caída del proceso después de actualizar el resultado, antes de aplicar el libro.
m,b,e,db=montar(); e.deseos=[lote(frac=.1)]
original=m._aplicar
m._aplicar=lambda *a,**k:(_ for _ in ()).throw(KeyboardInterrupt())
try:m.ciclo(T0,precios=dict(b.ultimo))
except KeyboardInterrupt:pass
m._aplicar=original
m._recuperar()
out['reinicio_estado_terminal_no_aplicado']={'exchange':b.posiciones(),'libro':libro_neto(m),'ordenes':db.filas('SELECT estado,aplicado FROM ordenes')}
# Una orden retorna aún abierta: se marca aplicada, después llena más y no se recupera.
m,b,e,db=montar(); e.deseos=[lote(frac=.1)]
original=b.enviar_orden
def abierta(*a,**k):
 b.llenar_solo=[3];r=original(*a,**k);r['estado']='abierta';return r
b.enviar_orden=abierta;ciclo(m,T0)
oid=list(b.hechas)[0];b._llenar('BTC',7,10);b.hechas[oid].update(estado='cerrada',llenado=10)
m._recuperar()
out['orden_abierta_tardia']={'exchange':b.posiciones(),'libro':libro_neto(m),'ordenes':db.filas('SELECT estado,aplicado FROM ordenes')}
# Reserva de balas se envía con precio de apertura ya pasado.
class Real:
 def __init__(self):self.calls=[]
 def aportar_margen(self,*a):self.calls.append(a)
r=Real();bl=Balas(Base(':memory:'),1000,real=r)
bl.st.update(activo=True,ntn=1,inv=1/100,mbtc=.0007,contrib=.07,resd=False,calentado=True,ultima_vela=None)
liq=bl._liq();t=pd.Timestamp('2026-01-07 04:00');s=pd.Series([101]*100,index=pd.date_range(end=t-H4,periods=100,freq='4h'))
# liq calculada sin ultimo_precio, .7%; apertura cerca; mínimo por debajo de liq pero reserva retroactiva evita liquidación.
bl.procesar(t,(95,100,90,100),s,bloqueado=True)
out['reserva_retroactiva']={'liq_sin_reserva':liq,'minimo':90,'liquidaciones':bl.st['liqs'],'reserva_enviada_al_cierre':r.calls,'reserva_usada':bl.st['resd']}
# Balas cambia el estado antes de confirmar una apertura real.
class Falla:
 def abrir(self,*a):raise TimeoutError('rechazada')
bl=Balas(Base(':memory:'),1000,real=Falla());bl.st.update(calentado=True,regWeekly=True,momT=True,levm=1)
cs=pd.Series([100]*99+[90],index=pd.date_range(end=t-H4,periods=100,freq='4h'))
try:bl.procesar(t,(100,100,90,90),cs)
except TimeoutError:pass
out['balas_apertura_fallida']={'activo':bl.st['activo'],'ntn':bl.st['ntn'],'ultima_vela':bl.st['ultima_vela']}
# Funding público: contrato solicitado para BTC.
class Ex:
 def fetch_funding_rate(self,s):self.symbol=s;return {'fundingRate':.001}
x=Ex();Publico(x).funding('BTC');out['funding_btc_contrato']=x.symbol
# Mini backtests del motor de medición, con gap/rebote y stop intrabar.
from validacion import motor_bt as MB
class E(FakeEst):
 def paso(self,t,*a):return [lote('a',frac=1,reescalable=False)] if False else []
class StopEst:
 nombre='fake'
 def decide_en(self,t):return True
 def paso(self,t,datos,st,ab,*a):
  if t==T0:return [lote('a',frac=1,reesc=False,stop_dist=10)]
  return [lote('a',frac=1,reesc=False,stop_dist=10)] if ab else []
orig=MB.todas;MB.todas=lambda:{'fake':StopEst()}
for name,bar in [('gap_rebote',(50,100,50,100)),('stop_cierre',(100,120,80,120))]:
 idx=pd.date_range(T0-H4,periods=5,freq='4h');rows=[(100,100,100,100),(100,100,100,100),bar,(100,100,100,100),(100,100,100,100)]
 def cargar(db):
  d=pd.DataFrame(rows,index=idx,columns=['o','h','l','c']);d['v']=1.;db.guardar_velas('BTC','4h',d)
  return ['BTC'],{k:d[[k]].rename(columns={k:'BTC'}) for k in d},None
 rr=MB.Corrida(None,None,None,None,{'fake':1},modo='PAPEL',corrida='B',capital=1000,desde=T0,hasta=T0+3*H4,cargar=cargar,mercados={'BTC':{'tam':1,'minimo':1,'id':'BTC'}},log_cada=0,sin_costos=True,cfg=__import__('cascada.config',fromlist=['Config']).Config(pesos={'fake':1},prioridad=['fake'],comision=0,corte_caida=9,alerta_caida=9)).correr()
 out[name]={'serie':rr.serie[['E','E_peor','E_mejor']].reset_index().astype({'t':str}).to_dict('records'),'drawdowns':{k:v for k,v in MB.metricas(rr.serie).items() if k.startswith('dd_')},'caja_final':rr.db.get('papel')['caja']}
MB.todas=orig
# Transferencia aceptada con respuesta perdida: la ruta alternativa vuelve a transferir.
from cascada.subcuenta import TransferidorSubcuenta
import time
class PrincipalTransferencias:
 def __init__(self):self.parent_to_sub=0;self.oid=[]
 def transfer(self,coin,monto,desde,hacia,params):
  if params.get('transferType')=='PARENT_TO_SUB':
   self.parent_to_sub+=monto;self.oid.append(params['clientOid'])
   if self.parent_to_sub==monto:raise TimeoutError('respuesta perdida después de transferir')
  return {'id':'ok'}
class Sub:
 def __init__(self):self.u=0.0
 def usdt_a_trading(self):pass
 def saldos(self):return dict(spot_main={'USDT':ex.parent_to_sub,'BTC':0.0},spot_trade={'USDT':0.0,'BTC':0.0},futuros_BTC=dict(total=0.0,libre=0.0))
ex=PrincipalTransferencias();ex.fetch_balance=lambda p:{'total':{'USDT':0.0}};tr=TransferidorSubcuenta('sub',Sub(),spot_principal=ex)
old_sleep=time.sleep;time.sleep=lambda *a:None
try:
 tr.enviar(100)
except Exception as e:out['_error_transferencia']=repr(e)
finally:time.sleep=old_sleep
out['transferencia_respuesta_perdida']={'solicitado':100,'transferido':ex.parent_to_sub,'clientOid_distintos':len(set(ex.oid))}
