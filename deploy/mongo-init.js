// Runs once, when the Mongo volume is first created: a least-privilege user for the app.
db = db.getSiblingDB("radar");
db.createUser({
  user: "radar",
  pwd: process.env.MONGO_APP_PASSWORD,
  roles: [{ role: "readWrite", db: "radar" }],
});
