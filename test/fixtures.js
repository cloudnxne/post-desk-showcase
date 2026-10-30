// A small, realistic client: a landscaping firm's sitemap and a new blog post for it.

export const SITEMAP_XML = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example-landscapes.co.uk/</loc></url>
  <url><loc>https://example-landscapes.co.uk/services/garden-design/</loc></url>
  <url><loc>https://example-landscapes.co.uk/services/patio-installation/</loc></url>
  <url><loc>https://example-landscapes.co.uk/services/artificial-grass/</loc></url>
  <url><loc><![CDATA[https://example-landscapes.co.uk/areas/watford/]]></loc></url>
  <url><loc>https://example-landscapes.co.uk/contact/?ref=a&amp;b=c</loc></url>
  <url><loc>https://example-landscapes.co.uk/services/garden-design/</loc></url>
  <url><loc>https://example-landscapes.co.uk/wp-content/uploads/hero.jpg</loc></url>
</urlset>`;

export const POST_MD = `# Planning Your Dream Garden

A good garden starts with a plan. Our garden design service maps out every bed, path and seating area before a spade goes in.

## Choosing a Patio

Natural stone is hard wearing and looks great for decades. We handle patio installation from groundwork to pointing.

#### Sealing your patio

Sealing protects the stone from stains.

## Low Maintenance Options

Things to consider:
- Artificial grass: stays green all year
- Gravel borders
- Raised beds

Steps we follow:
1. Survey the site
2. Agree the design
3. Build it

Get in touch for a free survey in Watford.`;

export const KNOWN = [
  "https://example-landscapes.co.uk/services/garden-design/",
  "https://example-landscapes.co.uk/services/patio-installation/",
  "https://example-landscapes.co.uk/services/artificial-grass/",
  "https://example-landscapes.co.uk/areas/watford/",
];
