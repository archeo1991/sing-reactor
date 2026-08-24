const { createWorker } = require('tesseract.js');

async function main() {
  const imagePaths = process.argv.slice(2);
  const lang = process.env.OCR_LANG || 'chi_sim+chi_tra+eng';
  if (!imagePaths.length) {
    console.error(JSON.stringify({ ok: false, error: 'missing_image' }));
    process.exit(2);
  }

  const worker = await createWorker(lang, 1, {
    logger: () => {},
  });

  try {
    await worker.setParameters({
      tessedit_pageseg_mode: '6',
      preserve_interword_spaces: '1',
    });
    const results = [];
    for (const imagePath of imagePaths) {
      const result = await worker.recognize(imagePath);
      const text = (result?.data?.text || '').replace(/\s+/g, ' ').trim();
      results.push({ path: imagePath, text });
    }
    console.log(JSON.stringify({ ok: true, results }, null, 0));
  } finally {
    await worker.terminate();
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error && error.message ? error.message : String(error) }));
  process.exit(1);
});
