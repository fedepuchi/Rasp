# De donde sale el numero de respiracion

**Resumen: no hay ningun sensor de respiracion en este equipo.** El valor de
RESP se **estima** a partir del pletismografo del MAX30102. Por eso en pantalla
lleva un `~` adelante y el cartel ESTIMADA, y en el JSON el campo se llama
`resp_rpm_estimated` y no `resp_rpm`.

Este documento explica como se estima, por que la estimacion tiene limites, y
que habria que agregar para medirla de verdad.

---

## 1. Como se estima hoy

Cuando una persona respira, la presion dentro del torax cambia y con ella el
retorno venoso. Eso hace que **la linea de base del canal infrarrojo del
MAX30102 suba y baje al ritmo de la respiracion**: un ondulado lento por debajo
de los pulsos cardiacos. En la literatura se lo conoce como *respiratory-induced
intensity variation* (RIIV).

El procesamiento esta en `processing/ppg.py`, dentro de `PpgProcessor.process()`
y `_track_respiration()`:

```
IR crudo (100 Hz, CON su continua)
   -> pasa-bajos anti-alias
   -> diezmado a 25 Hz
   -> pasabanda 0.1 - 0.7 Hz        (= 6 a 42 respiraciones por minuto)
   -> deteccion de picos
   -> mediana de los ultimos periodos
```

Dos detalles que importan:

- Se usa el infrarrojo **crudo, sin quitarle la continua**. La informacion
  respiratoria vive justamente en esa continua que el resto del procesamiento
  descarta. La cadena de SpO2, en cambio, usa un pasa-altos de 4 polos
  precisamente para sacarsela de encima.
- La banda de 0.1 a 0.7 Hz deja afuera los latidos (~1.2 Hz para arriba). Por
  eso el carril de RESP se ve como una onda lenta y limpia, sin el pulso.

### Otras dos modulaciones que existen y hoy no se usan

La respiracion deja huella en el PPG de tres formas distintas. Solo se explota
la primera:

| Modulacion | Que varia | Se usa |
|---|---|---|
| **RIIV** (intensidad) | la linea de base del IR | **si** |
| **RIAV** (amplitud) | la altura de cada pulso, por cambios en el volumen sistolico | no |
| **RIFV** (frecuencia) | los intervalos R-R, por arritmia sinusal respiratoria | no |

La tercera es interesante porque **no necesita el MAX30102 en absoluto**: la
frecuencia cardiaca se acelera al inspirar y se frena al espirar, asi que los
intervalos R-R que ya calcula el AD8232 llevan la respiracion adentro. Combinar
las tres fuentes da un numero bastante mas robusto que cualquiera sola, y es la
mejora mas barata posible: no hay que comprar nada.

---

## 2. Limites de la estimacion

### No entra en una medicion de 10 segundos

Es la limitacion mas visible: con `session.duration_s` en 10, RESP dice
**"sin dato en esta ventana"** y en el JSON sale `"n": 0` con los valores en
`null`. No es un error.

La cuenta:

| Etapa | Tiempo |
|---|---|
| asentar el pasa-altos de 0.1 Hz | ~12 s |
| 3 ciclos respiratorios a 16 rpm | ~11 s |
| **total** | **~25 a 30 s** |

Un pasa-altos de 0.1 Hz tiene una constante de tiempo de alrededor de
1/(2*pi*0.1) = 1.6 s, y hacen falta varias constantes para que el transitorio
baje a algo despreciable. No hay forma de acortarlo sin ensanchar la banda, y
ensancharla deja entrar deriva de la linea de base que se confunde con
respiracion.

Para que aparezca el numero:

```bash
python main.py --duracion 40
```

### Lo que la puede ensuciar

- **Movimiento de la mano.** Cualquier deriva lenta del contacto del dedo cae
  justo en la banda respiratoria y se cuenta como una respiracion.
- **Perfusion baja.** Si el indice de perfusion esta por debajo de ~0.5 %, la
  modulacion respiratoria queda tapada por el ruido.
- **Respiracion muy lenta o muy rapida.** Por debajo de 6 rpm o por encima de
  42 rpm el filtro la corta directamente.
- **Apnea.** El equipo no la detecta como tal: simplemente deja de encontrar
  picos y el numero se invalida a los pocos segundos. **No hay alarma de
  apnea**, y no seria honesto ponerla con esta senial.

---

## 3. Como se mide la respiracion de verdad

| Metodo | Que usa | Aplicable aca |
|---|---|---|
| **Impedancia toracica** | los mismos electrodos del ECG, inyectando corriente de alta frecuencia y midiendo como cambia la impedancia del torax al inflarse | **No.** El AD8232 no tiene el circuito de inyeccion ni de demodulacion. Es lo que usan los monitores de cabecera |
| **Capnografia** | mide CO2 en el aire exhalado | No, es caro y es otro proyecto |
| **Flujo nasal** | termistor o termocupla delante de la nariz | **Si**, muy barato |
| **Banda toracica** | sensor de estiramiento o piezo alrededor del pecho | **Si**, es lo mas directo |

---

## 4. Que habria que agregar

**El ADS1115 tiene tres canales libres.** Hoy solo se usa A0 para el AD8232, asi
que A1, A2 y A3 estan disponibles sin agregar ni un chip.

### Opcion A: banda toracica (la mas fiel)

Un sensor de estiramiento conductivo o un piezo alrededor del pecho, en un
divisor resistivo contra 3.3 V, entrando por A1. Mide el movimiento real de la
caja toracica, que es lo que uno quiere: es una medicion, no una inferencia.

- **A favor:** senial grande y limpia, inmune a que el paciente mueva la mano.
- **En contra:** hay que ponerse la banda, y se afloja si queda floja.

### Opcion B: termistor nasal (la mas barata)

Un NTC de 10k delante de la nariz, tambien en divisor contra 3.3 V. El aire
exhalado sale tibio y el inhalado entra frio, asi que la temperatura oscila con
cada ciclo.

- **A favor:** cuesta centavos y se arma en cinco minutos.
- **En contra:** la constante termica del NTC limita la frecuencia maxima, y se
  ensucia con cualquier corriente de aire del ambiente.

### Que habria que tocar en el codigo

Ninguna de las dos es un cambio grande, porque la estructura ya esta:

1. **`config.py`** — un bloque `RespSensorConfig` con el canal del ADS
   (`ads_channel: 1`), la frecuencia de muestreo (25 Hz alcanza y sobra) y la
   banda del filtro.
2. **`sensors/acquisition.py`** — el hilo del ECG ya lee el ADS1115; habria que
   alternar el multiplexor entre A0 y A1. **Ojo con esto:** el ADS1115 no
   multiplexa gratis. Cada cambio de canal en modo continuo obliga a descartar
   la primera conversion, y los 860 SPS del chip se reparten entre los dos
   canales. Con el ECG a 250 Hz y la respiracion a 25 Hz da comodo, pero hay
   que escribirlo con cuidado. La alternativa limpia es un segundo ADS1115 en
   0x49 (basta con llevar ADDR a VDD).
3. **`processing/`** — un procesador nuevo, mas simple que los que ya hay: es
   pasabanda y contar picos, sin deteccion adaptativa.
4. **`state.py` / `JSON.md`** — el campo pasaria a llamarse `resp_rpm` (medido)
   y `resp_rpm_estimated` quedaria como respaldo para cuando el sensor no este
   conectado.
5. **`ui/monitor.py`** — el carril de RESP pasaria a dibujar la senial real y
   se le sacaria el `~` y el cartel ESTIMADA.

---

## 5. En resumen

- Hoy: **estimada** del pletismografo, util para ver tendencias, no entra en
  una ventana de 10 s, y esta rotulada como estimacion en todos lados.
- Gratis: sumar la arritmia sinusal respiratoria de los intervalos R-R que ya
  se calculan, para que la estimacion sea mas robusta.
- Barato: banda toracica o termistor nasal por A1 del ADS1115, y ahi si pasa a
  ser una medicion.
- Fuera de alcance: impedancia toracica con el AD8232, que no puede hacerlo.
