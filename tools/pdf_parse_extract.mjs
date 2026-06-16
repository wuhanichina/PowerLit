/**
 * Extract plain text from a PDF file using pdf-parse 2.x (PDFParse), matching
 * Cherry Studio packages/shared/utils/pdf.ts (extractPdfText for buffer input).
 */
import { readFile } from 'node:fs/promises'
import { PDFParse } from 'pdf-parse'

async function main() {
  const pdfPath = process.argv[2]
  if (!pdfPath) {
    console.error('missing pdf path')
    process.exit(2)
  }

  let buffer
  try {
    buffer = await readFile(pdfPath)
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error))
    process.exit(1)
  }

  const parser = new PDFParse({ data: buffer })
  try {
    const result = await parser.getText()
    const text = typeof result?.text === 'string' ? result.text : ''
    process.stdout.write(text)
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error))
    process.exit(1)
  } finally {
    await parser.destroy()
  }
}

main()
