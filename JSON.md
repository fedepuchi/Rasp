# Contrato JSON del monitor

Todo lo que sale del Raspberry Pi va por HTTP a **un solo endpoint**. Quien
escriba el backend solo tiene que implementar esto.

## Regla de diseno: solo se manda lo que estos tres modulos miden

| Modulo | Que mide realmente | Que sale de ahi |
|---|---|---|
| **AD8232** | un canal de ECG (analogico) + LO+/LO- de electrodo suelto | `hr_bpm`, `rr_last_ms`, `hrv_rmssd_ms`, onda `ecg` |
| **ADS1115** | nada propio: es el conversor A/D del AD8232 | digitaliza el ECG; tambien detecta saturacion |
| **MAX30102** | luz roja e infrarroja reflejada + temperatura de su propio chip | `spo2_pct`, `pr_bpm`, `perfusion_index`, onda `pleth` |

Lo que **no** mide este equipo, y por lo tanto nunca aparece en `vitals`:
temperatura corporal, presion arterial, capnografia, y respiracion por
impedancia toracica o por flujo.

Hay dos casos de frontera que el JSON marca de forma explicita en vez de
disimularlos:

- **`resp_rpm_estimated`** — se estima a partir de como la respiracion mueve la
  linea de base del pletismografo. Es una tecnica real y documentada, pero es
  una **estimacion**, no una medicion. Por eso el campo lleva `_estimated` en
  el nombre.
- **`diagnostics.sensor_die_temp_c`** — es la temperatura del **chip**
  MAX30102, que el fabricante expone para compensar la deriva de los LED. No
  tiene nada que ver con la temperatura del paciente, y por eso vive en
  `diagnostics` y no en `vitals`.

```
POST  {backend.url}{backend.ingest_path}      por defecto  /api/v1/ingest
Content-Type: application/json
X-API-Key: <opcional, solo si backend.api_key esta configurado>
```

La respuesta solo tiene que ser un **2xx**. El cuerpo se ignora. Cualquier otra
cosa (o un timeout) se toma como fallo y el lote se reintenta.

---

## 1. Sobre (envelope)

Cada POST manda varios mensajes juntos para no abrir una conexion por dato:

```json
{
  "schema": "monitor.v1",
  "device_id": "rpi-monitor-01",
  "session_id": "a0e297f97cc042c28ed285f38dbae54d",
  "sent_at": "2026-08-05T04:48:10.902Z",
  "messages": [ { ... }, { ... } ]
}
```

| campo | tipo | que es |
|---|---|---|
| `schema` | string | version del contrato. Si algun dia cambia la estructura pasa a `monitor.v2` |
| `device_id` | string | que Raspberry mando esto. Configurable, por defecto `rpi-<hostname>` |
| `session_id` | string | UUID nuevo en cada arranque del programa. Sirve para agrupar |
| `sent_at` | string | ISO-8601 UTC del momento del POST (no de la medicion) |
| `messages` | array | 1 a 8 mensajes. Ver abajo |

### Campos comunes a todo mensaje

| campo | tipo | que es |
|---|---|---|
| `schema` | string | `"monitor.v1"` |
| `type` | string | `session_start` \| `vitals` \| `waveform` \| `event` \| `session_end` |
| `device_id`, `session_id` | string | idem sobre |
| `seq` | int | contador que arranca en 1 y no se repite dentro de una sesion |
| `ts` | string | ISO-8601 UTC con milisegundos, momento en que se armo el mensaje |
| `ts_ms` | int | lo mismo en milisegundos desde epoch (comodo para graficar) |
| `uptime_s` | float | segundos desde que arranco el programa |

> **Huecos y reintentos.** El `seq` es la unica forma confiable de saber si se
> perdio algo: si el backend ve 41, 42, 45 es porque el buffer offline del Pi
> descarto los lotes 43 y 44. Un lote se puede reenviar mas de una vez si el
> Pi no llego a recibir la respuesta, asi que conviene tratar
> `(device_id, session_id, seq)` como clave unica e idempotente.

---

## 2. `session_start`

Se manda una sola vez, al arrancar. Le dice al backend que esperar.

```json
{
  "schema": "monitor.v1",
  "type": "session_start",
  "device_id": "rpi-monitor-01",
  "session_id": "a0e297f9...",
  "seq": 1,
  "ts": "2026-08-05T04:47:46.485Z",
  "ts_ms": 1785905266485,
  "uptime_s": 0.012,
  "patient": { "id": "ANON-001", "name": "PACIENTE DE PRUEBA", "bed": "CAMA 1" },
  "sensors": {
    "ecg":  { "frontend": "AD8232", "adc": "ADS1115", "lead": "ECG",
              "fs_hz": 250, "gain_nominal": 1100.0, "calibrated": false },
    "spo2": { "sensor": "MAX30102", "fs_hz": 100.0, "calibrated": false }
  },
  "channels": [
    { "name": "ecg",   "unit": "mV",  "fs_hz": 250.0, "scale": 0.001,
      "note": "AD8232 → ADS1115. mV nominales con ganancia 1100, sin calibrar" },
    { "name": "pleth", "unit": "raw", "fs_hz": 100.0, "scale": 1.0,
      "note": "MAX30102, canal infrarrojo filtrado. Cuentas del ADC, sin unidad fisica" },
    { "name": "resp",  "unit": "raw", "fs_hz": 25.0,  "scale": 0.1,
      "note": "estimada de como la respiracion mueve la linea de base del pleth" }
  ],
  "measures": {
    "hr_bpm": "AD8232 → ADS1115 (intervalos R-R)",
    "spo2_pct": "MAX30102 (curva empirica generica, sin calibrar)",
    "resp_rpm_estimated": "estimado del pleth del MAX30102, NO medido"
  },
  "not_measured": [
    "temperatura corporal", "presion arterial", "capnografia",
    "respiracion por impedancia o flujo"
  ],
  "demo": false
}
```

Tres cosas que este mensaje declara a proposito:

- **`calibrated: false`** en los dos sensores. Los mV del ECG salen de dividir
  por la ganancia **nominal** del modulo (1100 en el diseno de referencia del
  AD8232), no de una calibracion contra un patron. El SpO2 usa una curva
  generica. Los numeros son utiles para ver tendencias, no son trazables.
- **`lead`** es como se rotula el trazo. Con 3 electrodos la derivacion depende
  de donde los pegue el operador, y el modulo no tiene forma de saberlo: por
  eso dice `"ECG"` y no `"ECG II"`, salvo que se configure a mano.
- **`demo: true`** significa que la senial es **simulada**, no viene de un
  paciente. Conviene que el backend la marque distinto o la descarte.

---

## 3. `vitals` — una vez por segundo

Es el mensaje principal: los numeros que se ven en pantalla.

```json
{
  "schema": "monitor.v1",
  "type": "vitals",
  "seq": 119,
  "ts": "2026-08-05T04:48:10.730Z",
  "ts_ms": 1785905290730,
  "uptime_s": 24.245,
  "patient": { "id": "ANON-001", "name": "PACIENTE DE PRUEBA", "bed": "CAMA 1" },
  "vitals": {
    "hr_bpm": 71,
    "hr_source": "ecg",
    "pr_bpm": 72,
    "pulse_deficit_bpm": -1,
    "spo2_pct": 98.1,
    "perfusion_index": 2.02,
    "rr_last_ms": 812.0,
    "hrv_rmssd_ms": 30.8,
    "resp_rpm_estimated": 17
  },
  "quality": {
    "ecg_leads_off": false,
    "ecg_lo_plus": false,
    "ecg_lo_minus": false,
    "ecg_noise": 0.047,
    "ecg_saturated": false,
    "finger_detected": true,
    "ecg_active": true,
    "ppg_active": true
  },
  "diagnostics": {
    "sensor_die_temp_c": 30.2,
    "ecg_baseline_v": 1.678,
    "ir_dc": 95245,
    "red_dc": 78416
  },
  "alarms": [],
  "alarms_muted": false,
  "limits": {
    "hr_bpm": [50, 120],
    "spo2_pct": [90, 100],
    "resp_rpm": [8, 30]
  }
}
```

### `vitals`

| campo | unidad | de que modulo sale | notas |
|---|---|---|---|
| `hr_bpm` | lpm | AD8232 → ADS1115 | mediana de los ultimos 8 intervalos R-R |
| `hr_source` | — | | `"ecg"`, o `null` si no hay medicion |
| `pr_bpm` | lpm | MAX30102 | frecuencia de pulso periferico |
| `pulse_deficit_bpm` | lpm | los dos | `hr_bpm - pr_bpm`. Ver abajo |
| `spo2_pct` | % | MAX30102 | curva empirica generica, sin calibrar |
| `perfusion_index` | % | MAX30102 | AC pico a pico sobre DC del infrarrojo. Tipico 0.2 a 5 |
| `rr_last_ms` | ms | AD8232 → ADS1115 | ultimo intervalo entre ondas R |
| `hrv_rmssd_ms` | ms | AD8232 → ADS1115 | RMSSD de corto plazo, ~30 latidos |
| `resp_rpm_estimated` | resp/min | MAX30102 | **estimado**, no medido |

**Cualquiera de estos puede venir en `null`**, y eso es informacion: significa
que en ese momento no habia una medicion confiable (sin dedo, electrodo suelto,
todavia no convergio). No lo trates como cero.

Dos aclaraciones que conviene tener presentes al guardar estos datos:

- **`pulse_deficit_bpm`** no es un error de medicion. `hr_bpm` cuenta
  contracciones electricas y `pr_bpm` cuenta pulsos que llegan al dedo: que se
  separen significa algo. En este equipo tambien puede ser simplemente ruido en
  alguno de los dos canales, asi que miralo junto con `quality`.
- **`hrv_rmssd_ms`** es de corto plazo. Un RMSSD "de manual" se calcula sobre 5
  minutos de registro limpio; este sale de ~30 latidos. Sirve para ver que se
  mueve, no para compararlo contra tablas de referencia.

### `quality`

Sirve para saber si un numero se puede creer:

| campo | significa |
|---|---|
| `ecg_leads_off` | algun electrodo despegado (LO+ u LO- del AD8232) |
| `ecg_lo_plus` / `ecg_lo_minus` | cual de los dos |
| `ecg_noise` | 0 limpio, 1 muy ruidoso (heuristico) |
| `ecg_saturated` | la salida del AD8232 esta pegada contra un riel: lo que se ve no es el corazon |
| `finger_detected` | hay dedo apoyado en el MAX30102 |
| `ecg_active` / `ppg_active` | el sensor arranco bien al iniciar |

### `diagnostics`

Salud del **equipo**. Nada de esto es un signo vital, y no deberia mostrarse
junto a los numeros del paciente:

| campo | unidad | que es |
|---|---|---|
| `sensor_die_temp_c` | °C | temperatura del chip MAX30102. Es para compensar los LED. **No es la del paciente** |
| `ecg_baseline_v` | V | continua a la salida del AD8232. Tiene que dar cerca de VCC/2 (~1.65 V con 3.3 V). Si da casi 0 o casi VCC, el frente analogico esta pegado a un riel |
| `ir_dc` | cuentas | nivel de continua del LED infrarrojo. Por debajo de ~50000 es que no hay dedo |
| `red_dc` | cuentas | idem del LED rojo |

### `alarms`

Lista de las alarmas activas, ordenadas de mas grave a menos:

```json
{
  "code": "SPO2_LOW",
  "level": "high",
  "message": "SATURACION BAJA",
  "value": 86.4,
  "limit": 90,
  "since": "2026-08-05T04:48:02.117Z",
  "duration_s": 8.6
}
```

| `level` | color en pantalla | que es |
|---|---|---|
| `high` | rojo | requiere accion inmediata |
| `medium` | ambar | atencion |
| `low` | celeste | informativa |
| `technical` | gris | problema del equipo, no del paciente |

Codigos posibles: `HR_LOW`, `HR_HIGH`, `NO_BEATS`, `SPO2_LOW`, `RESP_LOW`,
`RESP_HIGH`, `LEADS_OFF`, `ECG_SATURADO`, `NO_PROBE`, `NET_DOWN`.

`NO_BEATS` dice "sin latidos, revisar electrodos" y **no** dice "asistolia" a
proposito: un solo canal de ECG con electrodos de superficie no puede
distinguir un paro cardiaco de un electrodo flojo, y lo segundo es muchisimo
mas frecuente. Solo se emite despues de 15 segundos de funcionamiento, para no
dispararse mientras el detector de QRS todavia se esta asentando.

`alarms_muted: true` = alguien apreto silencio en el monitor. La alarma sigue
activa, solo no suena.

---

## 4. `waveform` — 4 veces por segundo

Las ondas para dibujar. Van como enteros con un factor de escala, que ocupa
bastante menos que mandar floats.

```json
{
  "schema": "monitor.v1",
  "type": "waveform",
  "seq": 120,
  "ts": "2026-08-05T04:48:10.980Z",
  "waveforms": [
    {
      "name": "ecg",
      "unit": "mV",
      "fs_hz": 250.0,
      "scale": 0.001,
      "t0": "2026-08-05T04:48:10.716Z",
      "t0_ms": 1785905290716,
      "n": 65,
      "samples": [0, -3, -6, -9, -10, -11, ...]
    },
    { "name": "pleth", "unit": "raw", "fs_hz": 100.0, "scale": 1.0,
      "t0_ms": 1785905290726, "n": 24, "samples": [103, 144, 196, ...] },
    { "name": "resp",  "unit": "raw", "fs_hz": 25.0,  "scale": 0.1,
      "t0_ms": 1785905290726, "n": 6,  "samples": [3046, 3027, ...] }
  ]
}
```

### Como reconstruir la onda

```js
valor_real  = samples[i] * scale          // en la unidad de "unit"
tiempo_ms   = t0_ms + (i * 1000 / fs_hz)  // instante de la muestra i
```

Para el ECG, `scale = 0.001` quiere decir que las muestras vienen en
**microvolts** y el valor real sale en mV. `pleth` y `resp` van en cuentas
crudas del ADC: sirven para dibujar la forma, no tienen unidad fisica.

Los bloques son **contiguos**: el `t0_ms` de uno arranca donde termina el
anterior. Si hay un salto, es que se perdio un lote (mira el `seq`).

### Cuanto trafico es

Con la configuracion por defecto: ~4 mensajes de onda + 1 de vitals por
segundo, alrededor de **4 kB/s (32 kbps)**. Nada para una WiFi. Si aun asi
sobra, `backend.ecg_send_decimation: 2` manda el ECG a la mitad de muestras.

---

## 5. `event` y `session_end`

```json
{ "type": "event", "event": "limites_cambiados", "detail": { "hr_high": 130 } }
{ "type": "session_end", "reason": "shutdown" }
```

`reason` puede ser `shutdown` (salida normal), `interrupt` (Ctrl+C) o
`captura`. Si el Pi se queda sin luz nunca llega un `session_end`: el backend
deberia dar por cerrada una sesion que no manda nada por, digamos, 30 segundos.

---

## 6. Que pasa si el backend se cae

1. El POST falla y el lote **no se descarta**: queda en una cola en RAM.
2. Se reintenta con backoff exponencial: 0.5 s, 1 s, 2 s... hasta 15 s.
3. La cola aguanta `offline_buffer_batches` lotes (600 por defecto, unos
   **2 minutos** de datos). Cuando se llena, se van tirando los mas viejos.
4. Cuando el backend vuelve, se manda todo lo acumulado en orden.
5. Mientras tanto la pantalla del Pi sigue funcionando normal: el pie de
   pantalla muestra `SIN SERVIDOR` y el tamano de la cola.

Nada de esto bloquea el dibujado: la red corre en su propio hilo.

---

## 7. Un backend minimo, para probar

**FastAPI** (Python):

```python
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/api/v1/ingest")
async def ingest(request: Request):
    envelope = await request.json()
    for message in envelope["messages"]:
        if message["type"] == "vitals":
            print(message["ts"], message["vitals"])
    return {"ok": True}
```

**Express** (Node):

```js
const express = require("express");
const app = express();
app.use(express.json({ limit: "2mb" }));   // los lotes de onda son grandes

app.post("/api/v1/ingest", (req, res) => {
  for (const m of req.body.messages) {
    if (m.type === "vitals") console.log(m.ts, m.vitals);
  }
  res.json({ ok: true });
});

app.listen(8000, "0.0.0.0");
```

Ojo con dos cosas al escribir el backend:

- **Escuchar en `0.0.0.0`**, no en `127.0.0.1`, o el Pi no lo va a poder ver
  desde la red.
- **Subir el limite del body**: un lote de 8 mensajes de onda puede pasar el
  100 kB por defecto de algunos frameworks.

Para probar sin escribir nada hay un receptor incluido:

```bash
python tools/receptor_prueba.py --port 8000 --guardar datos.jsonl
```
