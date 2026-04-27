(() => {
  const $ = (id) => document.getElementById(id);
  const state = {
    imageFile: null,
    imageUrl: '',
    markdown: '',
  };

  const text = {
    wait: '等待执行...',
    noImage: '未上传图片',
    imageReady: '已上传图片：',
    emptyInput: '请先输入问诊内容。',
    running: '正在处理...',
    failed: '请求失败：',
    copied: '已复制',
    copyFailed: '复制失败，请手动选择文本。',
  };

  function value(id) {
    const node = $(id);
    return node ? node.value.trim() : '';
  }

  function setText(id, content, empty = false) {
    const node = $(id);
    if (!node) return;
    node.textContent = content;
    node.classList.toggle('empty', empty);
  }

  function field(fields, key) {
    return fields?.[key] || '未提及';
  }

  function updateMeta(fields = {}) {
    setText('records-name', field(fields, 'name'));
    setText('records-sex', field(fields, 'sex'));
    setText('records-age', field(fields, 'age'));
    setText('records-visit-date', fields.visitDate || '未生成');
  }

  function previewSection(title, content) {
    return `
      <section class="record-preview-section">
        <h5>${window.oralUi.escapeHtml(title)}</h5>
        <p>${window.oralUi.escapeHtml(content || '未提及')}</p>
      </section>
    `;
  }

  function renderPreview(fields = {}) {
    const preview = $('records-preview');
    if (!preview) return;
    const esc = window.oralUi.escapeHtml;
    preview.innerHTML = `
      <article class="record-preview-doc">
        <section class="record-preview-section">
          <h5>基本信息</h5>
          <p>${esc(field(fields, 'name'))} / ${esc(field(fields, 'sex'))} / ${esc(field(fields, 'age'))} / ${esc(fields.visitDate || '未生成')}</p>
        </section>
        ${previewSection('主诉', field(fields, 'chiefComplaint'))}
        ${previewSection('现病史', field(fields, 'presentIllness'))}
        ${previewSection('既往史', field(fields, 'pastHistory'))}
        ${previewSection('药物过敏史', field(fields, 'allergyHistory'))}
        ${previewSection('专科检查', field(fields, 'exam'))}
        ${previewSection('辅助检查', field(fields, 'auxiliaryExam'))}
        ${previewSection('诊断', field(fields, 'diagnosis'))}
        ${previewSection('处理', field(fields, 'treatment'))}
        ${previewSection('注意事项', field(fields, 'advice'))}
        <div class="record-preview-sign">
          <span>医生签名：________</span>
          <span>复核：________</span>
        </div>
      </article>
    `;
  }

  async function correctConversation() {
    const raw = value('records-conversation');
    if (!raw) {
      setText('records-corrected', text.emptyInput, true);
      return null;
    }
    setText('records-corrected', text.running, true);
    const data = await window.oralUi.apiPost('/api/records/asr-correct', { conversation: raw });
    const corrected = data.result?.corrected_conversation || raw;
    setText('records-corrected', corrected);
    return corrected;
  }

  async function extractAndRender() {
    const raw = value('records-conversation');
    if (!raw) {
      setText('records-fields', text.emptyInput, true);
      return null;
    }
    setText('records-fields', text.running, true);
    const data = await window.oralUi.apiPost('/api/records/extract', { conversation: raw });
    const corrected = data.result?.corrected_conversation || raw;
    const fields = data.result?.fields_normalized || {};
    setText('records-corrected', corrected);
    updateMeta(fields);
    renderPreview(fields);
    setText('records-fields', JSON.stringify({ fields: data.result?.fields || fields, meta: data.result?.meta || {} }, null, 2));
    return fields;
  }

  async function generateRecord() {
    const raw = value('records-conversation');
    if (!raw) {
      setText('records-markdown', text.emptyInput, true);
      return null;
    }
    setText('records-corrected', text.running, true);
    setText('records-fields', text.running, true);
    setText('records-markdown', text.running, true);
    const data = await window.oralUi.apiPost('/api/records/workflow', { conversation: raw });
    const fields = data.result?.fields_normalized || {};
    const corrected = data.result?.corrected_conversation || raw;
    state.markdown = data.result?.markdown || '';
    setText('records-corrected', corrected);
    setText('records-fields', JSON.stringify({ fields: data.result?.fields || fields, meta: data.result?.meta || {} }, null, 2));
    updateMeta(fields);
    renderPreview(fields);
    setText('records-markdown', state.markdown);
    return data.result;
  }

  async function runSafely(task, targetId) {
    try {
      return await task();
    } catch (err) {
      setText(targetId, `${text.failed}${err.message}`, true);
      return null;
    }
  }

  function clearAll() {
    const input = $('records-conversation');
    if (input) input.value = '';
    setText('records-corrected', text.wait, true);
    setText('records-fields', text.wait, true);
    setText('records-image-analysis', text.noImage, true);
    setText('records-markdown', text.wait, true);
    setText('records-name', '待识别');
    setText('records-sex', '待识别');
    setText('records-age', '待识别');
    setText('records-visit-date', '待生成');
    const preview = $('records-preview');
    if (preview) preview.innerHTML = '<div class="record-preview-empty">等待生成病历</div>';
    state.markdown = '';
  }

  function clearImage() {
    if (state.imageUrl) URL.revokeObjectURL(state.imageUrl);
    state.imageUrl = '';
    state.imageFile = null;
    const fileInput = $('records-image-file');
    const img = $('records-image-preview');
    if (fileInput) fileInput.value = '';
    if (img) {
      img.removeAttribute('src');
      img.classList.remove('ready');
    }
    $('records-image-empty')?.classList.remove('hidden');
    $('records-image-clear')?.classList.add('hidden');
    setText('records-image-analysis', text.noImage, true);
  }

  function bind() {
    $('records-image-trigger')?.addEventListener('click', () => $('records-image-file')?.click());
    $('records-image-clear')?.addEventListener('click', clearImage);
    $('records-clear')?.addEventListener('click', () => {
      clearAll();
      clearImage();
    });
    $('records-correct')?.addEventListener('click', () => runSafely(correctConversation, 'records-corrected'));
    $('records-extract')?.addEventListener('click', () => runSafely(extractAndRender, 'records-fields'));
    $('records-generate')?.addEventListener('click', () => runSafely(generateRecord, 'records-markdown'));
    $('records-copy')?.addEventListener('click', async () => {
      const content = state.markdown || $('records-markdown')?.textContent || '';
      if (!content || content === text.wait) return;
      try {
        await navigator.clipboard.writeText(content);
        setText('records-markdown', `${content}\n\n${text.copied}`);
      } catch (_err) {
        setText('records-markdown', `${content}\n\n${text.copyFailed}`);
      }
    });
    $('records-image-file')?.addEventListener('change', (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      clearImage();
      state.imageFile = file;
      state.imageUrl = URL.createObjectURL(file);
      const img = $('records-image-preview');
      if (img) {
        img.src = state.imageUrl;
        img.classList.add('ready');
      }
      $('records-image-empty')?.classList.add('hidden');
      $('records-image-clear')?.classList.remove('hidden');
      setText('records-image-analysis', `${text.imageReady}${file.name}`);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
