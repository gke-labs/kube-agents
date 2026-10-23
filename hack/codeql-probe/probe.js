// Throwaway CodeQL probe: a js/code-injection finding on purpose. Not shipped; delete with the probe PR.
const express = require("express");
const app = express();
app.get("/run", (req, res) => {
  res.send(String(eval(req.query.code))); // js/code-injection on purpose
});
module.exports = app;
