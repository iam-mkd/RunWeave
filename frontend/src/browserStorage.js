// Accessors throw in some private modes; callers already treat failures as "no storage".
export const browserStorage = {
  getItem: (key) => window.localStorage.getItem(key),
  setItem: (key, value) => window.localStorage.setItem(key, value),
};
