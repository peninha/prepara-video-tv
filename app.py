"""Interface Windows para preparar vídeos localmente."""
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from engine import analyze, probe, prepare, tool, Cancelled


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Prepara Vídeo TV • PT + EN')
        self.geometry('980x780')
        self.minsize(800, 640)
        self.configure(bg='#f3f5f8')
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10))
        style.configure('TFrame', background='#f3f5f8')
        style.configure('TLabel', background='#f3f5f8')
        style.configure('Title.TLabel', font=('Segoe UI', 23, 'bold'), foreground='#16273a')
        style.configure('Accent.TButton', background='#176b62', foreground='white', padding=(16, 10))
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.busy = False
        self.files = []
        self.last_output = None
        self.controls = []
        outer = ttk.Frame(self, padding=24)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Seus vídeos, prontos para a TV', style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='Samsung NU7100  •  Até uma legenda PT e uma EN  •  Originais preservados').pack(anchor='w', pady=(6, 18))
        row = ttk.Frame(outer)
        row.pack(fill='x')
        self.button(row, 'Adicionar vídeos…', self.add_files).pack(side='left')
        self.button(row, 'Remover selecionados', self.remove_files).pack(side='left', padx=8)
        self.button(row, 'Limpar lista', self.clear_files).pack(side='left')
        self.listbox = tk.Listbox(outer, height=6, selectmode='extended', font=('Segoe UI', 10),
                                  bg='white', fg='#16273a', relief='flat', borderwidth=8)
        self.listbox.pack(fill='x', pady=(10, 14))
        self.mode = tk.StringVar(value='Automático — converter somente quando necessário')
        self.audio = tk.StringVar(value='Áudio principal do arquivo')
        for label, var, values in [
            ('Tratamento', self.mode, ['Automático — converter somente quando necessário', 'Só simplificar — preservar vídeo e áudio', 'Compatibilidade ampliada — converter vídeo e áudio']),
            ('Áudio a manter', self.audio, ['Áudio principal do arquivo', 'Preferir inglês', 'Preferir português'])]:
            row = ttk.Frame(outer)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=label, width=19).pack(side='left')
            combo = ttk.Combobox(row, textvariable=var, values=values, state='readonly')
            combo.pack(side='left', fill='x', expand=True)
            self.controls.append(combo)
        ttk.Label(outer, text='Português brasileiro tem preferência. A seleção depende dos idiomas informados no arquivo.').pack(anchor='w', pady=6)
        self.destination = tk.StringVar()
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text='Salvar cópias em', width=19).pack(side='left')
        entry = ttk.Entry(row, textvariable=self.destination)
        entry.pack(side='left', fill='x', expand=True)
        self.controls.append(entry)
        self.button(row, 'Escolher…', self.choose_folder).pack(side='left', padx=(8, 0))
        ttk.Label(outer, text='Em branco: cria uma pasta Preparados-TV ao lado de cada original.').pack(anchor='w')
        self.sample = tk.BooleanVar(value=False)
        self.verify = tk.BooleanVar(value=False)
        for text, var in [('Gerar apenas amostra dos primeiros 3 minutos', self.sample),
                          ('Verificar toda a decodificação da cópia (mais demorado)', self.verify)]:
            check = ttk.Checkbutton(outer, text=text, variable=var)
            check.pack(anchor='w', pady=(8, 0))
            self.controls.append(check)
        row = ttk.Frame(outer)
        row.pack(fill='x', pady=(18, 10))
        self.button(row, 'Analisar arquivos', lambda: self.start(False)).pack(side='left')
        self.button(row, 'Preparar para a TV', lambda: self.start(True), style='Accent.TButton').pack(side='left', padx=10)
        self.stop_button = ttk.Button(row, text='Cancelar', command=self.cancel, state='disabled')
        self.stop_button.pack(side='left')
        ttk.Button(row, text='Abrir resultados', command=self.open_results).pack(side='right')
        self.progress = ttk.Progressbar(outer, maximum=100)
        self.progress.pack(fill='x')
        self.status = tk.StringVar(value='Adicione um ou mais vídeos para começar.')
        ttk.Label(outer, textvariable=self.status).pack(anchor='w', pady=6)
        self.log = tk.Text(outer, height=9, wrap='word', font=('Segoe UI', 10), bg='white', relief='flat', padx=10, pady=10)
        self.log.pack(fill='both', expand=True)
        self.log.configure(state='disabled')
        ttk.Label(outer, text='A análise é preventiva; não garante reprodução na TV. Nenhum arquivo é enviado à internet.').pack(anchor='w', pady=(10, 0))
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(100, self.poll)
        self.add_log('Primeiro teste recomendado: modo Automático + amostra de 3 minutos.\n')

    def button(self, parent, text, command, **kwargs):
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self.controls.append(button)
        return button

    def add_files(self):
        paths = filedialog.askopenfilenames(title='Escolher vídeos', filetypes=[('Vídeos', '*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.m2ts *.webm *.wmv'), ('Todos os arquivos', '*.*')])
        for path in paths:
            if path not in self.files:
                self.files.append(path)
                self.listbox.insert('end', Path(path).name)

    def remove_files(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_files(self):
        self.files.clear()
        self.listbox.delete(0, 'end')

    def choose_folder(self):
        path = filedialog.askdirectory(title='Pasta para as cópias')
        if path:
            self.destination.set(path)

    def add_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def set_busy(self, value):
        self.busy = value
        for widget in self.controls:
            widget.configure(state='disabled' if value else ('readonly' if isinstance(widget, ttk.Combobox) else 'normal'))
        self.stop_button.configure(state='normal' if value else 'disabled')

    def start(self, write):
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo('Escolher vídeos', 'Adicione pelo menos um vídeo.')
            return
        try:
            tool('ffprobe')
            tool('ffmpeg')
        except Exception as exc:
            messagebox.showerror('Ferramenta não encontrada', str(exc))
            return
        mode = {'Automático': 'auto', 'Só simplificar': 'copy', 'Compatibilidade ampliada': 'compat'}[self.mode.get().split(' — ')[0]]
        audio = {'Áudio principal do arquivo': 'original', 'Preferir inglês': 'en', 'Preferir português': 'pt'}[self.audio.get()]
        options = (list(self.files), mode, audio, self.destination.get().strip(), 180 if self.sample.get() else None, self.verify.get(), write)
        self.cancel_event.clear()
        self.set_busy(True)
        self.progress['value'] = 0
        threading.Thread(target=self.worker, args=options, daemon=True).start()

    def worker(self, paths, mode, audio, destination, seconds, verify, write):
        successes, failures = 0, 0
        try:
            for i, path in enumerate(paths, 1):
                if self.cancel_event.is_set():
                    break
                name = Path(path).name
                self.events.put(('status', f'{i}/{len(paths)} — Analisando {name}'))
                try:
                    plan = analyze(path, probe(path), mode, audio)
                    self.events.put(('log', '\n' + name + '\n' + '\n'.join('• ' + note for note in plan.notes + plan.blockers)))
                    if write:
                        folder = Path(destination) if destination else Path(path).parent / 'Preparados-TV'
                        self.events.put(('status', f'{i}/{len(paths)} — Preparando {name}'))
                        output = prepare(plan, folder, self.cancel_event, lambda n: self.events.put(('progress', n)), seconds, verify)
                        self.events.put(('output', str(output.parent)))
                        self.events.put(('log', 'SALVO: ' + str(output)))
                    successes += 1
                except Cancelled:
                    self.events.put(('log', 'Cancelado. Cópias já concluídas foram mantidas.'))
                    break
                except Exception as exc:
                    failures += 1
                    self.events.put(('log', 'ERRO: ' + str(exc)))
            state = 'Cancelado' if self.cancel_event.is_set() else ('Preparação concluída' if write else 'Análise concluída')
            self.events.put(('status', f'{state}: {successes} concluído(s), {failures} com erro.'))
        finally:
            self.events.put(('done', None))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    self.add_log(value)
                elif kind == 'status':
                    self.status.set(value)
                elif kind == 'progress':
                    if value < 0:
                        self.status.set('Verificando a decodificação completa da cópia…')
                    else:
                        self.progress['value'] = value
                elif kind == 'output':
                    self.last_output = value
                elif kind == 'done':
                    self.set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def cancel(self):
        self.cancel_event.set()
        self.status.set('Cancelando; aguardando a operação atual encerrar…')

    def open_results(self):
        if self.last_output and Path(self.last_output).is_dir():
            os.startfile(self.last_output)
        else:
            messagebox.showinfo('Resultados', 'Nenhuma cópia foi criada nesta sessão ainda.')

    def close(self):
        if self.busy:
            self.cancel()
            self.after(300, self.close)
        else:
            self.destroy()


if __name__ == '__main__':
    App().mainloop()
