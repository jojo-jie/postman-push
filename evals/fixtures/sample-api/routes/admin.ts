import express from "express";

const app = express();

// List admin audit events.
app.get("/admin/audit", (req, res) => {
  const { page, size } = req.query;
  const tenant = req.headers["x-tenant-id"];
  res.json({ page, size, tenant });
});
