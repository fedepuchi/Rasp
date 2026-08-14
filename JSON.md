# Contrato de telemetria hacia SIAPPC

El monitor publica por **MQTT sobre TLS**. No hay endpoint HTTP: la telemetria
que llega a la base de SIAPPC entra por el broker y la consume
`backend/src/services/mqttIngest.ts`.

> Versiones anteriores de este documento describian un sobre `monitor.v1` que se
> mandaba por `POST /api/v1/ingest`. **Ese endpoint no existe.** Si tenes codigo
> escrito contra aquello, esta seccion es lo que hay que seguir.

```
Broker   mqtts://<host>:8883        solo TLS, con usuario, sin anonimos
Temas    siappc/<device>/telemetry  una lectura por mensaje, QoS 1
         siappc/<device>/status     retenido, mas Last Will
```

`<device>` es `DEVICE_CODE`, y tiene que coincidir con `dispositivo.codigo` en
la base. Si el backend no lo encuentra ahi **descarta las lecturas**, porque no
sabe a que hospital colgarlas.

---

## 1. Telemetria

Un mensaje por lectura. Plano, sin anidar:

```json
{
  "device": "RPI-01",
  "variable": "spo2",
  "value": 97.4,
  "unit": "%",
  "ts": 1786427133.884,
  "hash": "3f2a...64 caracteres hexadecimales..."
}
```

| campo | tipo | notas |
|---|---|---|
| `device` | string (1-50) | `dispositivo.codigo` en la base |
| `variable` | string (1-60) | ver la tabla de abajo. No es un enum cerrado, pero estos nombres importan |
| `value` | number finito | ni `null`, ni `NaN`, ni texto |
| `unit` | string (1-20) | manda el catalogo: si `variable.unidad` ya existe con otra unidad, se guarda igual y queda un warning |
| `ts` | number | **Unix en segundos**, con decimales. No milisegundos ni ISO-8601 |
| `hash` | string de **exactamente 64** | SHA-256 en hexadecimal. Ver abajo |

### Las variables que publica este monitor

| `variable` | `unit` | De donde sale |
|---|---|---|
| `hr` | `bpm` | AD8232 → ADS1115, intervalos R-R |
| `spo2` | `%` | MAX30102, relacion rojo/infrarrojo |
| `pr` | `bpm` | MAX30102, picos del pletismografo |
| `perfusion` | `%` | MAX30102, AC pico a pico sobre DC |
| `resp` | `rpm` | **estimada** del pleth, no medida |

`hr` y `spo2` tienen que llamarse asi: son los nombres que ya usa
`iot/src/main.py` y sobre los que `evaluateAlert` decide las alertas.

**Un signo vital que no se pudo medir no se publica.** No se manda cero ni un
valor de relleno: simplemente no hay fila para ese instante. Un `0` de SpO2
seria una emergencia inexistente.

### El hash no es opcional

Es la identidad de la lectura, y la base tiene un **indice unico** sobre esa
columna:

```python
raw = f"{device}|{variable}|{timestamp:.3f}|{value:.4f}"
hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

Los tres decimales de `ts` y los cuatro de `value` son parte del contrato: si se
formatea distinto, el mismo dato genera hashes distintos y deja de deduplicar.

Gracias a eso, un reenvio del buffer local tras una caida —o un duplicado de
QoS 1— choca contra el indice y se descarta con `INSERT IGNORE`, en vez de
contarse dos veces.

---

## 2. Estado del dispositivo

```json
{ "device": "RPI-01", "status": "online" }
```

Se publica **retenido** en `siappc/<device>/status`, con `status: "online"` al
conectar y `"offline"` al cerrar ordenadamente.

El mismo mensaje va como **Last Will**: si el Pi se queda sin luz o sin red, el
broker lo publica por su cuenta y el tablero marca el equipo como caido sin
esperar un timeout.

Al ser retenido, un backend que arranca despues se pone al dia con los equipos
que ya estaban conectados.

---

## 3. Lo que NO se publica, y por que

**Las ondas.** El ECG son 250 muestras por segundo y la tabla `lectura` guarda
una fila por valor: no entra en ese modelo. Graficar la onda en el tablero
necesita su propia tabla y su propio tema. Mientras tanto las muestras se
dibujan en pantalla y ahi se quedan. Es la misma limitacion que tiene
`iot/src/main.py`, que publica una muestra instantanea de ECG y no la onda.

**Las alarmas de pantalla.** El payload es una lectura suelta y no tiene donde
llevarlas. El backend evalua las suyas sobre `hr`, `spo2`, `pr` y `resp`; las
del monitor siguen sonando en el Pi. Los limites de `resp` son los mismos en los
dos lados a proposito, para que no digan cosas distintas.

**La calidad de senial y el diagnostico del equipo.** Electrodo suelto,
saturacion del ECG, nivel de los LED: nada de eso tiene lugar en el payload.
Vive en la pantalla, en la tecla `D` y en `tools/diagnostico.py`.

---

## 4. Si el broker no esta

1. La lectura se escribe en una cola SQLite local (`monitor-buffer.db`) y el
   bucle principal sigue. **El dibujado nunca espera a la red.**
2. Un hilo aparte vacia la cola. Una lectura se borra **solo** cuando el broker
   confirma la entrega con el PUBACK de QoS 1.
3. La cola aguanta `BUFFER_MAX_ROWS` lecturas (86400 por defecto, mas o menos un
   dia a una por segundo). Cuando se llena tira las **mas viejas**: ante una
   caida larga importan mas las recientes.
4. Al reconectar sale todo lo acumulado, y el hash evita que se cuente dos veces
   lo que si habia llegado.

En pantalla, el pie muestra el estado del broker y cuantas lecturas hay en la
cola.

---

## 5. Configuracion

Todo sale del entorno o de un `.env`, nunca del codigo:

| Variable | Default | Que es |
|---|---|---|
| `DEVICE_CODE` | `RPI-01` | tiene que existir en `dispositivo.codigo` |
| `MQTT_HOST` | `localhost` | IP o nombre del broker |
| `MQTT_PORT` | `8883` | el de TLS |
| `MQTT_USER` / `MQTT_PASSWORD` | — | el broker no acepta anonimos |
| `MQTT_TLS` | `true` | solo se baja para un broker heredado |
| `MQTT_CA_FILE` | `certs/ca.crt` | CA que firma el certificado del broker |
| `MQTT_CLIENT_CERT_FILE` / `_KEY_FILE` | — | solo si el broker exige mTLS |
| `PUBLISH_INTERVAL` | `1` | segundos entre tandas |
| `BUFFER_MAX_ROWS` | `86400` | techo de la cola local |

**Dentro de SIAPPC** (en `iot/monitor/`) estos valores no se duplican: se leen
de `iot/.env` a traves de `iot/src/config.py`, para que broker, credenciales y CA
esten en un solo sitio. **Fuera de SIAPPC** se leen del entorno o de un `.env`
al lado del monitor. Lo resuelve `iot_env.py`, y los nombres son los mismos en
los dos casos.

Si el broker se alcanza por IP, esa IP tiene que estar en el certificado como
SAN, o el Pi lo rechaza. No se desactiva la validacion:

```bash
MQTT_EXTRA_SANS="IP:192.168.1.50" sh infra/mosquitto/gen-certs.sh
```

---

## 6. Probarlo sin levantar el backend

```bash
python tools/receptor_prueba.py --host 192.168.0.50
```

Se suscribe a los mismos temas que el backend, imprime lo que llega y **valida
cada payload con las mismas reglas que `telemetrySchema`**. Si algo no valida lo
dice: si no, el backend lo descarta con un warning en su log y desde el Pi no se
nota nada.
