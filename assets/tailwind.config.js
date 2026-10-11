/** Tailwind build for the dashboard. Rebuild after editing templates:
 *  tailwindcss -c assets/tailwind.config.js -i assets/app.css -o clusterx_os/static/app.css --minify
 */
module.exports = {
  content: ["./clusterx_os/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
