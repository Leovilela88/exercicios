/* MultiFit — gerador de cards de compartilhamento (conquistas e treinos).
   Card 9:16 (1080x1920) para Stories, com foto opcional reposicionável,
   modo de fundo transparente e métricas por esporte com ícones. */
(function () {
    const ATHLETE = window.MF_ATHLETE || '';
    const W = 1080, H = 1920;

    // ---------------------------------------------------------------- estado
    let payload = null;       // { type, ... }
    let photoImg = null;      // Image da foto escolhida (ou null)
    let transparent = false;  // só a base da conquista, fundo transparente
    let offX = 0, offY = 0;   // deslocamento da foto (espaço do canvas)

    // ---------------------------------------------------------------- DOM
    let overlay, canvas, ctx, fileInput, photoBtn, photoLabel, transWrap,
        transInput, dragHint;

    function buildSheet() {
        overlay = document.createElement('div');
        overlay.className = 'sheet-overlay share-sheet';
        overlay.hidden = true;
        overlay.innerHTML = `
            <div class="sheet">
                <div class="sheet-handle"></div>
                <div class="share-preview-wrap">
                    <canvas id="mf-share-canvas" width="${W}" height="${H}"></canvas>
                </div>
                <p class="share-hint" data-role="drag" hidden>Arraste a foto para posicionar</p>
                <label class="share-toggle">
                    <input type="checkbox" data-role="trans" />
                    <span>Fundo transparente (sticker para Stories)</span>
                </label>
                <input type="file" accept="image/*" data-role="file" hidden />
                <div class="share-actions">
                    <button type="button" class="btn-ghost" data-role="photo">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.6-3.6a2 2 0 0 0-2.8 0L6 20"/></svg>
                        <span data-role="photo-label">Adicionar foto</span>
                    </button>
                    <button type="button" class="btn-primary" data-role="do">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
                        Compartilhar
                    </button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        canvas = overlay.querySelector('#mf-share-canvas');
        ctx = canvas.getContext('2d');
        fileInput = overlay.querySelector('[data-role="file"]');
        photoBtn = overlay.querySelector('[data-role="photo"]');
        photoLabel = overlay.querySelector('[data-role="photo-label"]');
        transWrap = overlay.querySelector('.share-toggle');
        transInput = overlay.querySelector('[data-role="trans"]');
        dragHint = overlay.querySelector('[data-role="drag"]');

        overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
        photoBtn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', onPickPhoto);
        transInput.addEventListener('change', () => {
            transparent = transInput.checked;
            syncControls();
            render();
        });
        overlay.querySelector('[data-role="do"]').addEventListener('click', doShare);
        bindDrag();
    }

    function syncControls() {
        const showPhoto = !transparent;
        photoBtn.style.display = showPhoto ? '' : 'none';
        dragHint.hidden = !(showPhoto && photoImg);
    }

    // ---------------------------------------------------------------- logo
    const logo = new Image();
    logo.crossOrigin = 'anonymous';
    let logoReady = false;
    logo.onload = () => { logoReady = true; if (overlay && !overlay.hidden) render(); };
    logo.src = '/static/logo-icon.png';

    document.fonts && document.fonts.ready.then(() => {
        if (overlay && !overlay.hidden) render();
    }).catch(() => {});

    // ---------------------------------------------------------------- util
    function roundRect(x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y, x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r);
        ctx.arcTo(x, y, x + w, y, r);
        ctx.closePath();
    }

    function wrap(text, max) {
        const words = (text || '').split(' ');
        const out = [];
        let line = '';
        for (const w of words) {
            const t = line ? line + ' ' + w : w;
            if (ctx.measureText(t).width > max && line) { out.push(line); line = w; }
            else line = t;
        }
        if (line) out.push(line);
        return out;
    }

    function coverDraw(img) {
        const r = Math.max(W / img.naturalWidth, H / img.naturalHeight);
        const w = img.naturalWidth * r, h = img.naturalHeight * r;
        const x0 = (W - w) / 2, y0 = (H - h) / 2;
        // clampa o deslocamento pra foto sempre cobrir o quadro
        offX = Math.min(0 - x0, Math.max(W - w - x0, offX));
        offY = Math.min(0 - y0, Math.max(H - h - y0, offY));
        ctx.drawImage(img, x0 + offX, y0 + offY, w, h);
    }

    // ---------------------------------------------------------------- ícones
    // desenho vetorial simples (centrado em x,y; s = "meio-tamanho")
    function drawIcon(name, x, y, s, color) {
        ctx.save();
        ctx.translate(x, y);
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = Math.max(3, s * 0.16);
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.beginPath();
        switch (name) {
            case 'clock':
                ctx.arc(0, 0, s, 0, Math.PI * 2); ctx.stroke();
                ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(0, -s * 0.55);
                ctx.moveTo(0, 0); ctx.lineTo(s * 0.42, s * 0.1); ctx.stroke();
                break;
            case 'distance': // seta de navegação
                ctx.moveTo(0, -s); ctx.lineTo(s * 0.72, s);
                ctx.lineTo(0, s * 0.38); ctx.lineTo(-s * 0.72, s);
                ctx.closePath(); ctx.stroke();
                break;
            case 'pace': // pulso/atividade
                ctx.moveTo(-s, 0); ctx.lineTo(-s * 0.45, 0);
                ctx.lineTo(-s * 0.15, -s * 0.75); ctx.lineTo(s * 0.15, s * 0.75);
                ctx.lineTo(s * 0.45, 0); ctx.lineTo(s, 0); ctx.stroke();
                break;
            case 'flame':
                ctx.moveTo(0, -s);
                ctx.bezierCurveTo(s * 0.72, -s * 0.3, s * 0.72, s * 0.65, 0, s);
                ctx.bezierCurveTo(-s * 0.72, s * 0.65, -s * 0.72, -s * 0.3, 0, -s);
                ctx.stroke();
                break;
            case 'speed': // velocímetro
                ctx.arc(0, s * 0.25, s, Math.PI, Math.PI * 2); ctx.stroke();
                ctx.beginPath(); ctx.moveTo(0, s * 0.25);
                ctx.lineTo(s * 0.5, -s * 0.45); ctx.stroke();
                ctx.beginPath(); ctx.arc(0, s * 0.25, s * 0.12, 0, Math.PI * 2); ctx.fill();
                break;
            case 'volume': { // halter
                const pw = s * 0.34, ph = s * 1.4;
                roundRect(-s, -ph / 2, pw, ph, pw * 0.4); ctx.stroke();
                roundRect(s - pw, -ph / 2, pw, ph, pw * 0.4); ctx.stroke();
                ctx.beginPath(); ctx.moveTo(-s + pw, 0); ctx.lineTo(s - pw, 0); ctx.stroke();
                break;
            }
            case 'count': // lista
                for (const yy of [-s * 0.6, 0, s * 0.6]) {
                    ctx.moveTo(-s * 0.3, yy); ctx.lineTo(s, yy);
                }
                ctx.stroke();
                for (const yy of [-s * 0.6, 0, s * 0.6]) {
                    ctx.beginPath(); ctx.arc(-s * 0.7, yy, s * 0.13, 0, Math.PI * 2); ctx.fill();
                }
                break;
            default:
                ctx.arc(0, 0, s, 0, Math.PI * 2); ctx.stroke();
        }
        ctx.restore();
    }

    // ---------------------------------------------------------------- layout
    function layout(bottomY) {
        const m = { brandY: bottomY };
        m.athleteY = m.brandY - 56;
        m.divY = m.athleteY - 46;

        if (payload.type === 'badge') {
            ctx.font = '400 38px Inter, sans-serif';
            m.lines = wrap(payload.desc, 880);
            m.bodyBottom = m.divY - 64;
            m.bodyTop = m.bodyBottom - (m.lines.length - 1) * 50;
            m.titleY = m.bodyTop - 72;
        } else {
            m.metricsLabelY = m.divY - 58;
            m.metricsValueY = m.metricsLabelY - 42;
            m.metricsIconCy = m.metricsValueY - 74;
            m.bodyTop = m.metricsIconCy - 30;
            m.titleY = m.bodyTop - 62;
        }
        m.labelY = m.titleY - 78;
        m.dotCy = m.labelY - 52;
        m.topY = m.dotCy - 15;
        return m;
    }

    function drawBrand(cx, y) {
        const txt = 'MultiFit · multifit.app.br';
        ctx.font = '600 30px Inter, sans-serif';
        ctx.textAlign = 'left';
        const tw = ctx.measureText(txt).width;
        const lh = 36, lw = logoReady ? logo.width * (lh / logo.height) : 0;
        const gap = lw ? 12 : 0;
        const total = lw + gap + tw;
        const x = cx - total / 2;
        if (logoReady) ctx.drawImage(logo, x, y - lh + 8, lw, lh);
        ctx.fillStyle = '#93c5fd';
        ctx.fillText(txt, x + lw + gap, y);
        ctx.textAlign = 'center';
    }

    function drawContent(m) {
        const cx = W / 2;
        const color = payload.color;

        // selo (ponto)
        ctx.beginPath();
        ctx.arc(cx, m.dotCy, 15, 0, Math.PI * 2);
        ctx.fillStyle = color; ctx.fill();

        // rótulo (conquista / data)
        ctx.fillStyle = color;
        ctx.font = '600 32px Inter, sans-serif';
        ctx.textAlign = 'center';
        if ('letterSpacing' in ctx) ctx.letterSpacing = '2px';
        const labelTxt = payload.type === 'badge'
            ? 'CONQUISTA DESBLOQUEADA'
            : (payload.dateLabel || '').toUpperCase();
        if (labelTxt) ctx.fillText(labelTxt, cx, m.labelY);
        if ('letterSpacing' in ctx) ctx.letterSpacing = '0px';

        // título
        ctx.fillStyle = '#ffffff';
        ctx.font = '800 86px Inter, sans-serif';
        ctx.fillText(payload.type === 'badge' ? payload.title : payload.sportLabel, cx, m.titleY);

        // corpo: descrição (badge) ou métricas (treino)
        if (payload.type === 'badge') {
            ctx.fillStyle = '#cbd5e1';
            ctx.font = '400 38px Inter, sans-serif';
            m.lines.forEach((ln, i) => {
                ctx.fillText(ln, cx, m.bodyBottom - (m.lines.length - 1 - i) * 50);
            });
        } else {
            const mts = payload.metrics || [];
            const n = mts.length || 1;
            const colW = Math.min(290, 960 / n);
            const startX = cx - (colW * n) / 2 + colW / 2;
            mts.forEach((mt, i) => {
                const x = startX + i * colW;
                drawIcon(mt.icon, x, m.metricsIconCy, 25, color);
                ctx.fillStyle = '#ffffff';
                ctx.font = '800 48px Inter, sans-serif';
                ctx.fillText(mt.value, x, m.metricsValueY);
                ctx.fillStyle = '#94a3b8';
                ctx.font = '600 24px Inter, sans-serif';
                if ('letterSpacing' in ctx) ctx.letterSpacing = '1px';
                ctx.fillText((mt.label || '').toUpperCase(), x, m.metricsLabelY);
                if ('letterSpacing' in ctx) ctx.letterSpacing = '0px';
            });
        }

        // divisor
        ctx.strokeStyle = 'rgba(148,163,184,0.28)';
        ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(cx - 200, m.divY); ctx.lineTo(cx + 200, m.divY); ctx.stroke();

        // atleta
        ctx.fillStyle = '#e2e8f0';
        ctx.font = '700 36px Inter, sans-serif';
        ctx.fillText(ATHLETE, cx, m.athleteY);

        // marca (logo pequeno + nome)
        drawBrand(cx, m.brandY);
    }

    function draw() {
        if (!payload) return;
        const cx = W / 2, color = payload.color;
        ctx.clearRect(0, 0, W, H);

        if (transparent) {
            // sticker: bloco centralizado sobre painel translúcido
            const m0 = layout(0);
            const height = m0.brandY - m0.topY;
            const bottomY = H / 2 + height / 2;
            const m = layout(bottomY);

            const panelW = 940, panelX = cx - panelW / 2;
            const panelTop = m.topY - 46, panelBot = m.brandY + 40;
            roundRect(panelX, panelTop, panelW, panelBot - panelTop, 40);
            ctx.fillStyle = 'rgba(9,14,26,0.55)';
            ctx.fill();
            ctx.lineWidth = 2;
            ctx.strokeStyle = 'rgba(148,163,184,0.30)';
            ctx.stroke();

            const glow = ctx.createRadialGradient(cx, (panelTop + panelBot) / 2, 30,
                cx, (panelTop + panelBot) / 2, 520);
            glow.addColorStop(0, color + '24');
            glow.addColorStop(1, 'rgba(0,0,0,0)');
            ctx.save(); roundRect(panelX, panelTop, panelW, panelBot - panelTop, 40);
            ctx.clip(); ctx.fillStyle = glow; ctx.fillRect(0, 0, W, H); ctx.restore();

            drawContent(m);
            return;
        }

        // fundo: foto (cover) ou gradiente
        if (photoImg && photoImg.naturalWidth) {
            coverDraw(photoImg);
        } else {
            const g = ctx.createLinearGradient(0, 0, W, H);
            g.addColorStop(0, '#0b1530');
            g.addColorStop(1, '#050810');
            ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
        }

        // degradê azul: transparente em cima -> escuro embaixo
        const og = ctx.createLinearGradient(0, 0, 0, H);
        og.addColorStop(0.00, 'rgba(8,14,32,0)');
        og.addColorStop(0.42, 'rgba(8,14,32,0.12)');
        og.addColorStop(0.62, 'rgba(9,17,42,0.74)');
        og.addColorStop(1.00, 'rgba(4,7,15,0.99)');
        ctx.fillStyle = og; ctx.fillRect(0, 0, W, H);

        // brilho da cor de acento atrás do bloco
        const glow = ctx.createRadialGradient(cx, 1520, 40, cx, 1520, 660);
        glow.addColorStop(0, color + '22');
        glow.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = glow; ctx.fillRect(0, 1000, W, H - 1000);

        drawContent(layout(H - 96));
    }

    async function render() {
        try { await document.fonts.ready; } catch (_) {}
        draw();
    }

    // ---------------------------------------------------------------- foto
    function onPickPhoto() {
        const f = fileInput.files && fileInput.files[0];
        if (!f) return;
        const img = new Image();
        img.onload = () => {
            photoImg = img; offX = 0; offY = 0;
            photoLabel.textContent = 'Trocar foto';
            syncControls();
            render();
        };
        img.src = URL.createObjectURL(f);
    }

    function bindDrag() {
        let dragging = false, lastX = 0, lastY = 0;
        const scale = () => W / (canvas.getBoundingClientRect().width || W);
        canvas.addEventListener('pointerdown', (e) => {
            if (transparent || !photoImg) return;
            dragging = true; lastX = e.clientX; lastY = e.clientY;
            try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
        });
        canvas.addEventListener('pointermove', (e) => {
            if (!dragging) return;
            const s = scale();
            offX += (e.clientX - lastX) * s;
            offY += (e.clientY - lastY) * s;
            lastX = e.clientX; lastY = e.clientY;
            draw();
        });
        const end = () => { dragging = false; };
        canvas.addEventListener('pointerup', end);
        canvas.addEventListener('pointercancel', end);
    }

    // ---------------------------------------------------------------- ações
    function dataUrlToFile(url, name) {
        const [head, b64] = url.split(',');
        const mime = (head.match(/:(.*?);/) || [])[1] || 'image/png';
        const bin = atob(b64);
        const arr = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
        return new File([arr], name, { type: mime });
    }

    function download(url, name) {
        const a = document.createElement('a');
        a.href = url; a.download = name; a.click();
    }

    // Importante: gera a imagem de forma SÍNCRONA (toDataURL) e chama
    // navigator.share na mesma "tarefa" do clique — o Safari/iOS cancela o
    // compartilhamento se houver um await entre o gesto e o share.
    function doShare() {
        const name = payload.type === 'badge' ? 'conquista-multifit.png' : 'treino-multifit.png';
        const text = payload.type === 'badge'
            ? `Desbloqueei a conquista "${payload.title}" no MultiFit! 💪`
            : `Treino de ${payload.sportLabel} registrado no MultiFit! 💪`;
        let dataUrl;
        try { dataUrl = canvas.toDataURL('image/png'); }
        catch (_) { return; }
        const file = dataUrlToFile(dataUrl, name);
        const data = { files: [file], title: 'MultiFit', text };
        if (navigator.canShare && navigator.canShare(data)) {
            navigator.share(data).catch((err) => {
                // se o usuário não cancelou, oferece o download como alternativa
                if (err && err.name !== 'AbortError') download(dataUrl, name);
            });
            return;
        }
        download(dataUrl, name);
    }

    function open(data) {
        if (!overlay) buildSheet();
        payload = data;
        photoImg = null; offX = 0; offY = 0; transparent = false;
        fileInput.value = '';
        transInput.checked = false;
        photoLabel.textContent = 'Adicionar foto';
        // modo transparente só faz sentido pra conquistas (sticker)
        transWrap.style.display = payload.type === 'badge' ? '' : 'none';
        syncControls();
        overlay.hidden = false;
        render();
    }

    function close() { if (overlay) overlay.hidden = true; }

    // ---------------------------------------------------------------- bind
    function bindButtons(root) {
        (root || document).querySelectorAll('[data-mf-share]').forEach((btn) => {
            if (btn._mfBound) return;
            btn._mfBound = true;
            btn.addEventListener('click', () => {
                try { open(JSON.parse(btn.dataset.mfShare)); } catch (_) {}
            });
        });
    }

    window.MultiFitShare = { open, bind: bindButtons };
    document.addEventListener('DOMContentLoaded', () => bindButtons());
    bindButtons();
})();
