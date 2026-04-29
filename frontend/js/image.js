(() => {
  const { byId, escapeHtml } = window.oralUi;

  let selectedFile = null;
  let objectUrl = null;
  let latestBoxes = [];

  function setOutput(text, isEmpty = false) {
    const output = byId('image-output');
    if (!output) return;
    output.textContent = text;
    output.classList.toggle('empty', isEmpty);
  }

  function clearOverlay() {
    const overlay = byId('image-overlay');
    if (overlay) overlay.innerHTML = '';
    latestBoxes = [];
  }

  function imageDisplayRect() {
    const stage = byId('image-upload-trigger');
    const preview = byId('image-preview');
    if (!stage || !preview || !preview.naturalWidth || !preview.naturalHeight) {
      return null;
    }
    const width = stage.clientWidth;
    const height = stage.clientHeight;
    const scale = Math.min(width / preview.naturalWidth, height / preview.naturalHeight);
    const renderedWidth = preview.naturalWidth * scale;
    const renderedHeight = preview.naturalHeight * scale;
    return {
      left: (width - renderedWidth) / 2,
      top: (height - renderedHeight) / 2,
      width: renderedWidth,
      height: renderedHeight,
    };
  }

  function renderBoxes(boxes) {
    latestBoxes = Array.isArray(boxes) ? boxes : [];
    const overlay = byId('image-overlay');
    if (!overlay) return;
    overlay.innerHTML = '';
    const rect = imageDisplayRect();
    if (!rect || !latestBoxes.length) return;

    latestBoxes.forEach((box) => {
      const x1 = Number(box.x1);
      const y1 = Number(box.y1);
      const x2 = Number(box.x2);
      const y2 = Number(box.y2);
      if (![x1, y1, x2, y2].every(Number.isFinite)) return;

      const node = document.createElement('div');
      node.className = 'image-box';
      node.style.left = `${rect.left + x1 * rect.width}px`;
      node.style.top = `${rect.top + y1 * rect.height}px`;
      node.style.width = `${Math.max(8, (x2 - x1) * rect.width)}px`;
      node.style.height = `${Math.max(8, (y2 - y1) * rect.height)}px`;

      const label = document.createElement('span');
      label.className = 'image-box-label';
      const suffix = box.confidence ? ` / ${box.confidence}` : '';
      label.innerHTML = escapeHtml(`${box.label || '异常区域'}${suffix}`);
      node.appendChild(label);
      overlay.appendChild(node);
    });
  }

  function formatResult(payload) {
    if (!payload || payload.ok === false) {
      return payload?.message || '影像识别失败。';
    }
    const result = payload.result || {};
    const sections = [];
    if (result.report_text) {
      sections.push(result.report_text);
    } else if (result.raw_model_output) {
      sections.push(result.raw_model_output);
    }

    const boxCount = Array.isArray(result.boxes) ? result.boxes.length : 0;
    sections.push(`\n定位框：${boxCount ? `${boxCount} 个` : '未返回可绘制坐标'}`);
    return sections.join('\n').trim();
  }

  async function uploadImage() {
    const button = byId('image-run');
    const query = byId('image-query')?.value.trim() || '';
    if (!selectedFile) {
      setOutput('请先上传一张口腔影像。');
      return;
    }

    const form = new FormData();
    form.append('file', selectedFile);
    form.append('query', query);
    form.append('allow_web', 'true');

    button.disabled = true;
    button.textContent = '识别中...';
    clearOverlay();
    setOutput('正在上传影像并调用云端视觉模型，请稍候...');

    try {
      const response = await fetch('/api/image/analyze-upload', {
        method: 'POST',
        body: form,
      });
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json') ? await response.json() : { message: await response.text() };
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.message || `${response.status} ${response.statusText}`);
      }
      setOutput(formatResult(payload));
      renderBoxes(payload.result?.boxes || []);
    } catch (error) {
      setOutput(`调用失败：${error.message || String(error)}`);
    } finally {
      button.disabled = false;
      button.textContent = '开始识别';
    }
  }

  function previewFile(file) {
    selectedFile = file;
    clearOverlay();
    setOutput('等待分析...', true);

    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = URL.createObjectURL(file);

    const preview = byId('image-preview');
    const empty = byId('image-empty');
    if (!preview || !empty) return;
    preview.onload = () => renderBoxes(latestBoxes);
    preview.src = objectUrl;
    preview.classList.add('ready');
    empty.classList.add('hidden');
  }

  function boot() {
    const input = byId('image-file');
    const trigger = byId('image-upload-trigger');
    const run = byId('image-run');

    trigger?.addEventListener('click', () => input?.click());
    input?.addEventListener('change', () => {
      const file = input.files?.[0];
      if (!file) return;
      if (!file.type.startsWith('image/')) {
        setOutput('请选择图片文件。');
        return;
      }
      previewFile(file);
    });
    run?.addEventListener('click', uploadImage);
    window.addEventListener('resize', () => renderBoxes(latestBoxes));
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
