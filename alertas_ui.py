"""
alertas_ui.py
--------------
Todo lo puramente VISUAL de las alertas (pantalla completa + voz), sin
ninguna dependencia de Evolution/Ollama/SEDAPAL. Lo usan tanto monitor.py
(modo standalone, todo en una sola PC) como cliente_alertas.py (las PCs
de la red local que solo reciben y muestran lo que les manda el backend).

Requisitos: pip install pyttsx3 Pillow (tkinter viene incluido con Python
en Windows).
"""

import base64
import io
import queue
import threading


def hablar(texto):
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.say(texto)
        engine.runAndWait()
    except Exception as e:
        print(f"[WARN] No se pudo reproducir voz: {e}")


def mostrar_alerta(remitente, texto, imagen_b64=None, segundos_auto_cierre=30):
    """
    Ventana de pantalla completa roja avisando una emergencia, con una
    barra que se llena en "segundos_auto_cierre" y al terminar cierra la
    ventana sola (o ESC/ENTER para cerrarla antes). Es una funcion
    BLOQUEANTE (corre su propio mainloop de tkinter): quien la llame debe
    hacerlo desde un hilo dedicado, nunca desde el hilo principal que
    tiene que seguir atendiendo otras cosas.
    """
    import tkinter as tk

    job_barra = [None]

    def cerrar(event=None):
        if job_barra[0] is not None:
            try:
                root.after_cancel(job_barra[0])
            except Exception:
                pass
        root.destroy()

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.configure(bg="#7a1f1f")
    root.bind("<Escape>", cerrar)
    root.bind("<Return>", cerrar)

    tk.Label(
        root, text="EMERGENCIA DETECTADA", fg="white", bg="#7a1f1f",
        font=("Segoe UI", 60, "bold"),
    ).pack(pady=(60, 20))

    tk.Label(
        root, text=f"De: {remitente}", fg="#f5c6c6", bg="#7a1f1f",
        font=("Segoe UI", 26),
    ).pack(pady=(0, 30))

    tk.Label(
        root, text=texto, fg="white", bg="#7a1f1f",
        font=("Segoe UI", 32, "bold"), wraplength=1200, justify="center",
    ).pack(padx=60, pady=20)

    if imagen_b64:
        try:
            from PIL import Image, ImageTk

            datos = base64.b64decode(imagen_b64)
            img = Image.open(io.BytesIO(datos))
            img.thumbnail((450, 450))
            foto_tk = ImageTk.PhotoImage(img)

            label_foto = tk.Label(root, image=foto_tk, bg="#7a1f1f")
            label_foto.image = foto_tk  # referencia viva, si no el garbage collector la borra
            label_foto.pack(pady=10)
        except Exception as e:
            print(f"[WARN] no se pudo mostrar la miniatura de la foto: {e}")

    tk.Label(
        root, text="Se cierra sola. Presiona ESC o ENTER para cerrar antes.",
        fg="#f5c6c6", bg="#7a1f1f", font=("Segoe UI", 18),
    ).pack(side="bottom", pady=(10, 20))

    # barra de progreso: se llena en segundos_auto_cierre y al terminar cierra la ventana sola
    ancho_barra, alto_barra, pasos = 900, 24, 100
    canvas = tk.Canvas(root, width=ancho_barra, height=alto_barra, bg="#4a1414", highlightthickness=0)
    canvas.pack(side="bottom", pady=(0, 10))
    barra = canvas.create_rectangle(0, 0, 0, alto_barra, fill="#f5c6c6", width=0)
    intervalo_ms = int(segundos_auto_cierre * 1000 / pasos)

    def avanzar_barra(paso=0):
        if paso > pasos:
            cerrar()
            return
        try:
            canvas.coords(barra, 0, 0, int(ancho_barra * paso / pasos), alto_barra)
        except tk.TclError:
            return  # la ventana ya se cerro manualmente
        job_barra[0] = root.after(intervalo_ms, avanzar_barra, paso + 1)

    avanzar_barra()

    # la voz se lanza en un hilo aparte para no bloquear la ventana
    threading.Thread(target=hablar, args=(f"Alerta de emergencia. {texto}",), daemon=True).start()

    root.mainloop()


class PantallaDesconexion:
    """
    Pantalla completa NEGRA con letras VERDES: avisa que la sesion de
    WhatsApp se cerro y hay que reescanear el QR. A diferencia de
    mostrar_alerta(), esta ventana vive DURANTE TODA la ejecucion del
    programa (se crea oculta al arrancar) y se muestra/actualiza/oculta
    segun los eventos que le lleguen desde afuera - pensada para que quien
    la controla (el backend, via HTTP) mande un QR nuevo cada pocos
    segundos mientras la sesion siga caida, y un aviso para ocultarla
    cuando vuelva a conectar.

    tkinter solo se puede tocar de forma segura desde el hilo que corre su
    propio mainloop: por eso actualizar()/ocultar() son seguras de llamar
    desde CUALQUIER otro hilo (se comunican por una cola interna que el
    hilo de la ventana va revisando solo).
    """

    def __init__(self):
        self._cola = queue.Queue()
        self._listo = threading.Event()
        self._hilo = threading.Thread(target=self._loop, daemon=True)
        self._hilo.start()
        self._listo.wait(timeout=10)

    def actualizar(self, qr_base64, estado):
        self._cola.put(("actualizar", qr_base64, estado))

    def ocultar(self):
        self._cola.put(("ocultar", None, None))

    def _loop(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.configure(bg="black")
        # no se cierra con la X: solo desaparece cuando el backend avisa que reconecto
        root.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Label(
            root, text="SESION DE WHATSAPP CERRADA", fg="#00ff41", bg="black",
            font=("Consolas", 60, "bold"),
        ).pack(pady=(50, 10))
        tk.Label(
            root,
            text="Escanea el codigo QR con el celular (WhatsApp > Dispositivos vinculados)\n"
                 "para volver a iniciar sesion",
            fg="#00ff41", bg="black", font=("Consolas", 26, "bold"), justify="center",
        ).pack(pady=(0, 20))

        label_qr = tk.Label(root, bg="black")
        label_qr.pack(pady=20)

        label_estado = tk.Label(root, text="", fg="#00ff41", bg="black", font=("Consolas", 18))
        label_estado.pack(side="bottom", pady=20)

        def _actualizar_qr(qr_b64, estado):
            if qr_b64:
                try:
                    from PIL import Image, ImageTk

                    datos = base64.b64decode(qr_b64)
                    img = Image.open(io.BytesIO(datos)).resize((480, 480))
                    foto_tk = ImageTk.PhotoImage(img)
                    label_qr.configure(image=foto_tk)
                    label_qr.image = foto_tk
                    label_estado.configure(text=f"Estado: {estado or 'desconocido'} - el QR se refresca solo")
                except Exception as e:
                    label_estado.configure(text=f"No se pudo dibujar el QR ({e})")
            else:
                label_estado.configure(text=f"Esperando codigo QR... Estado: {estado or 'desconocido'}")

        def drenar():
            try:
                while True:
                    accion, qr_b64, estado = self._cola.get_nowait()
                    if accion == "ocultar":
                        root.withdraw()
                    elif accion == "actualizar":
                        root.deiconify()
                        _actualizar_qr(qr_b64, estado)
            except queue.Empty:
                pass
            root.after(300, drenar)

        root.after(300, drenar)
        self._listo.set()
        root.mainloop()
